import Foundation
import CryptoKit

final class HubTransport: @unchecked Sendable {
    let policy: HubPolicy
    private let configuration: URLSessionConfiguration
    private let lock = NSLock()
    private var generation = 0
    private var transfers: [UUID: HubTransfer] = [:]
    static var decoder: JSONDecoder { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }

    init(policy: HubPolicy, configuration: URLSessionConfiguration = .default) {
        self.policy = policy
        self.configuration = configuration
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 25
        configuration.timeoutIntervalForResource = 180
    }

    func endpoint(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard !path.hasPrefix("/"), !path.contains(".."), !path.contains("\\"),
              let url = URL(string: path, relativeTo: policy.baseURL)?.absoluteURL,
              policy.isInternal(url), var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw HubNativeError.untrustedURL }
        if !query.isEmpty { components.queryItems = query }
        guard let result = components.url else { throw HubNativeError.untrustedURL }
        return result
    }
    func request(_ path: String, method: String = "GET", csrf: String? = nil, query: [URLQueryItem] = []) throws -> URLRequest {
        var request = URLRequest(url: try endpoint(path, query: query))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("BYBCHubIOS/0.2.0", forHTTPHeaderField: "User-Agent")
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        request.httpShouldHandleCookies = false
        if let cookies = HTTPCookieStorage.shared.cookies(for: request.url!), !cookies.isEmpty {
            request.setValue(HTTPCookie.requestHeaderFields(with: cookies)["Cookie"], forHTTPHeaderField: "Cookie")
        }
        if let csrf { request.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token") }
        return request
    }
    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        let (data, response) = try await perform(request(path, query: query))
        return try decode(data, response)
    }
    func post<T: Decodable>(_ path: String, body: [String: Any], csrf: String? = nil) async throws -> T {
        var request = try request(path, method: "POST", csrf: csrf)
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await perform(request)
        return try decode(data, response)
    }
    func logoutRequest(csrf: String?) throws -> URLRequest { try request("api/mobile/logout", method: "POST", csrf: csrf) }
    func logout(using request: URLRequest) async { _ = try? await perform(request, commitCookies: false) }
    func upload(project: String, conversation: Int?, draft: HubDraft, csrf: String, disk: HubDisk) async throws -> HubReceipt {
        let boundary = "Hub-" + UUID().uuidString
        let bodyURL = disk.temporaryURL(suffix: "upload")
        FileManager.default.createFile(atPath: bodyURL.path, contents: nil, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        defer { try? FileManager.default.removeItem(at: bodyURL) }
        let handle = try FileHandle(forWritingTo: bodyURL)
        func write(_ text: String) throws { try handle.write(contentsOf: Data(text.utf8)) }
        func field(_ name: String, _ value: String) throws { try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n") }
        do {
            try field("description", draft.text)
            try field("conversation_id", conversation.map(String.init) ?? "")
            try field("request_key", draft.requestKey)
            for agent in draft.agentIDs { try field("agent_ids", String(agent)) }
            for item in draft.files {
                try Task.checkCancellation()
                let url = disk.fileURL(item.id)
                guard FileManager.default.fileExists(atPath: url.path) else { throw HubNativeError.missingFile }
                let safe = HubPolicy.safeFilename(item.name).replacingOccurrences(of: "\"", with: "_")
                try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"attachments\"; filename=\"\(safe)\"\r\nContent-Type: \(item.mimeType)\r\n\r\n")
                let incoming = try FileHandle(forReadingFrom: url)
                defer { try? incoming.close() }
                while let chunk = try incoming.read(upToCount: 64 * 1024), !chunk.isEmpty { try handle.write(contentsOf: chunk) }
                try write("\r\n")
            }
            try write("--\(boundary)--\r\n")
            try handle.close()
        } catch { try? handle.close(); throw error }
        var request = try request("api/mobile/projects/\(project)/messages", method: "POST", csrf: csrf)
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await perform(request, upload: bodyURL)
        return try decode(data, response)
    }
    func download(_ attachment: HubAttachment, disk: HubDisk) async throws -> URL {
        guard let url = URL(string: attachment.downloadUrl, relativeTo: policy.baseURL)?.absoluteURL,
              policy.isInternal(url), policy.isDownload(url) else { throw HubNativeError.untrustedURL }
        let directory = disk.temporaryURL(suffix: "preview")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(HubPolicy.safeFilename(attachment.name))
        do {
            var request = try self.request("api/mobile/session")
            request.url = url; request.setValue("application/octet-stream, */*", forHTTPHeaderField: "Accept")
            _ = try await perform(request, destination: destination, expectedBytes: attachment.byteSize)
            if let checksum = attachment.sha256, !checksum.isEmpty {
                let incoming = try FileHandle(forReadingFrom: destination); defer { try? incoming.close() }
                var hash = SHA256()
                while let chunk = try incoming.read(upToCount: 1024 * 1024), !chunk.isEmpty { try Task.checkCancellation(); hash.update(data: chunk) }
                guard hash.finalize().map({ String(format: "%02x", $0) }).joined() == checksum.lowercased() else { throw HubNativeError.rejected("文件完整性检查失败，请重新下载。") }
            }
            return destination
        } catch { try? FileManager.default.removeItem(at: directory); throw error }
    }
    private func decode<T: Decodable>(_ data: Data, _ response: URLResponse) throws -> T {
        guard let response = response as? HTTPURLResponse else { throw HubNativeError.invalidResponse }
        if response.statusCode == 401 { throw HubNativeError.unauthorized }
        guard (200..<300).contains(response.statusCode) else {
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw HubNativeError.rejected((payload?["detail"] as? String) ?? "请求没有完成，请稍后重试。")
        }
        guard response.mimeType == "application/json" else { throw HubNativeError.incompatibleServer }
        do { return try Self.decoder.decode(T.self, from: data) } catch { throw HubNativeError.invalidResponse }
    }
    func cancelAll() {
        lock.lock(); generation += 1; let pending = Array(transfers.values); transfers.removeAll(); lock.unlock()
        pending.forEach { $0.cancel() }
    }
    private func registration(_ transfer: HubTransfer, id: UUID) -> Int {
        lock.lock(); defer { lock.unlock() }; transfers[id] = transfer; return generation
    }
    private func finish(_ id: UUID) { lock.lock(); transfers.removeValue(forKey: id); lock.unlock() }
    private func acceptCookies(_ response: HTTPURLResponse, generation expected: Int) throws {
        lock.lock(); defer { lock.unlock() }
        guard generation == expected else { throw CancellationError() }
        guard (200..<300).contains(response.statusCode), response.mimeType == "application/json", let url = response.url, policy.isInternal(url) else { return }
        var headers: [String: String] = [:]
        for (key, value) in response.allHeaderFields { if let key = key as? String, let value = value as? String { headers[key] = value } }
        for cookie in HTTPCookie.cookies(withResponseHeaderFields: headers, for: url)
            where cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == policy.baseURL.host?.lowercased()
                && policy.baseURL.path.hasPrefix(cookie.path) { HTTPCookieStorage.shared.setCookie(cookie) }
    }
    private func perform(_ request: URLRequest, upload: URL? = nil, destination: URL? = nil, expectedBytes: Int? = nil, commitCookies: Bool = true) async throws -> (Data, HTTPURLResponse) {
        let transfer = HubTransfer(configuration: configuration, request: request, upload: upload, destination: destination, expectedBytes: expectedBytes)
        let id = UUID()
        let token = registration(transfer, id: id)
        defer { finish(id) }
        let result = try await withTaskCancellationHandler(operation: { try await transfer.run() }, onCancel: { transfer.cancel() })
        if commitCookies { try acceptCookies(result.1, generation: token) }
        return result
    }
}

// Each operation owns its delegate and bounded sink. Cookie storage is disabled
// in URLSession; a cancelled account generation cannot commit Set-Cookie.
private final class HubTransfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock(), configuration: URLSessionConfiguration, request: URLRequest
    private let upload: URL?, destination: URL?, expectedBytes: Int?
    private var session: URLSession?, task: URLSessionTask?, response: HTTPURLResponse?
    private var continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>?
    private var cancelled = false, finished = false, data = Data(), received = 0
    private var handle: FileHandle?, failure: Error?
    private var limit: Int { destination == nil ? 16 * 1024 * 1024 : 2 * 1024 * 1024 * 1024 }
    init(configuration: URLSessionConfiguration, request: URLRequest, upload: URL?, destination: URL?, expectedBytes: Int?) {
        self.configuration = configuration; self.request = request; self.upload = upload; self.destination = destination; self.expectedBytes = expectedBytes
    }
    func run() async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock(); defer { lock.unlock() }
            guard !cancelled else { continuation.resume(throwing: CancellationError()); return }
            self.continuation = continuation
            let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil); self.session = session
            if let upload { task = session.uploadTask(with: request, fromFile: upload) }
            else { task = session.dataTask(with: request) }
            task?.resume()
        }
    }
    func cancel() { lock.lock(); cancelled = true; let task = task; lock.unlock(); task?.cancel() }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        do {
            guard let http = response as? HTTPURLResponse else { throw HubNativeError.invalidResponse }
            self.response = http
            if http.statusCode == 401 { throw HubNativeError.unauthorized }
            guard !(300..<400).contains(http.statusCode) else { throw HubNativeError.invalidResponse }
            guard response.expectedContentLength <= Int64(limit) else { throw HubNativeError.rejected("文件或回复超过本机接收上限。") }
            if let destination {
                guard (200..<300).contains(http.statusCode), http.mimeType != "text/html", http.mimeType != "application/json" else { throw HubNativeError.invalidResponse }
                if let expectedBytes, expectedBytes > 0, response.expectedContentLength >= 0, response.expectedContentLength != Int64(expectedBytes) { throw HubNativeError.rejected("文件大小与记录不符，请重新获取。") }
                FileManager.default.createFile(atPath: destination.path, contents: nil, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
                handle = try FileHandle(forWritingTo: destination)
            } else if http.mimeType != "application/json" { throw HubNativeError.incompatibleServer }
            completionHandler(.allow)
        } catch { failure = error; completionHandler(.cancel) }
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        do {
            received += data.count
            guard received <= limit, destination == nil || expectedBytes == nil || expectedBytes! <= 0 || received <= expectedBytes! else { throw HubNativeError.rejected("文件大小异常，已停止下载。") }
            if let handle { try handle.write(contentsOf: data) } else { self.data.append(data) }
        } catch { failure = error; dataTask.cancel() }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        try? handle?.close()
        lock.lock(); guard !finished else { lock.unlock(); return }; finished = true
        let continuation = self.continuation; self.continuation = nil; let wasCancelled = cancelled; lock.unlock()
        defer { session.invalidateAndCancel(); self.session = nil; self.task = nil }
        if let failure { continuation?.resume(throwing: failure) }
        else if wasCancelled { continuation?.resume(throwing: CancellationError()) }
        else if let error { continuation?.resume(throwing: error) }
        else if let response {
            if response.expectedContentLength >= 0 && response.expectedContentLength != Int64(received) || destination != nil && expectedBytes != nil && expectedBytes! > 0 && expectedBytes! != received { continuation?.resume(throwing: HubNativeError.rejected("文件未完整接收，请重试。")) }
            else { continuation?.resume(returning: (data, response)) }
        } else { continuation?.resume(throwing: HubNativeError.invalidResponse) }
    }
}
