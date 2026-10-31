import Foundation
import CryptoKit

struct HubDisk {
    let root: URL
    init(baseURL: URL, root: URL? = nil) {
        let base = root ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("HubNativeV1", isDirectory: true)
        self.root = base.appendingPathComponent(Self.digest(baseURL.absoluteString), isDirectory: true)
        try? FileManager.default.createDirectory(at: self.root, withIntermediateDirectories: true)
        var protected = self.root
        var attributes = URLResourceValues(); attributes.isExcludedFromBackup = true
        try? protected.setResourceValues(attributes)
    }
    static func digest(_ text: String) -> String { SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined() }
    private func url(_ key: String) -> URL { root.appendingPathComponent(Self.digest(key) + ".json") }
    func save<T: Encodable>(_ value: T, key: String) throws {
        let data = try JSONEncoder().encode(value)
        try data.write(to: url(key), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
    func load<T: Decodable>(_ type: T.Type, key: String) -> T? { (try? Data(contentsOf: url(key))).flatMap { try? JSONDecoder().decode(type, from: $0) } }
    func remove(_ key: String) { try? FileManager.default.removeItem(at: url(key)) }
    func fileURL(_ id: UUID) -> URL { root.appendingPathComponent(id.uuidString + ".attachment") }
    func temporaryURL(suffix: String) -> URL { root.appendingPathComponent(UUID().uuidString + "." + suffix) }
    func importFile(data: Data, name: String, mime: String, existing: [HubDraftFile]) throws -> HubDraftFile {
        guard HubNativeRules.acceptsFiles(existing: existing, newBytes: data.count) else { throw HubNativeError.oversizedFile }
        let result = HubDraftFile(id: UUID(), name: HubPolicy.safeFilename(name), mimeType: mime, bytes: data.count)
        try data.write(to: fileURL(result.id), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        return result
    }
    func clear() {
        // Only this app-owned, server-hashed directory is removed on sign-out.
        try? FileManager.default.removeItem(at: root)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
}
struct HubCached<T: Codable>: Codable {
    let session: String
    let savedAt: Date
    let value: T
    func read(session: String, now: Date = Date()) -> T? {
        guard !session.isEmpty, self.session == session, savedAt <= now, now.timeIntervalSince(savedAt) < 7 * 86400 else { return nil }
        return value
    }
}
struct HubRememberedIdentity: Codable { let identity: HubIdentity; let cookieDigest: String }
