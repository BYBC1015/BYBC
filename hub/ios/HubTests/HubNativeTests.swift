import XCTest
@testable import Hub

final class HubNativeTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }
    func testRealAPIContractsDecodeWithoutSchemaAssumptions() throws {
        let decoder = HubTransport.decoder
        let workspace = try decoder.decode(HubWorkspace.self, from: fixture("workspace"))
        XCTAssertEqual(workspace.project.slug, "workspace-team")
        XCTAssertEqual(workspace.devices.first?.deviceId, 1)
        XCTAssertEqual(workspace.agents.first?.active, true)
        XCTAssertFalse(workspace.messages.isEmpty)
        XCTAssertNotNil(workspace.revision)
        _ = try decoder.decode(HubProjectList.self, from: fixture("projects"))
        _ = try decoder.decode(HubTaskDetail.self, from: fixture("task"))
        _ = try decoder.decode(HubDevices.self, from: fixture("devices"))
        _ = try decoder.decode(HubLibrary.self, from: fixture("library"))
        _ = try decoder.decode(HubReleases.self, from: fixture("releases"))
        XCTAssertEqual(try decoder.decode(HubToolbox.self, from: fixture("toolbox")).tools.count, 4)
    }
    func testPrivateDiskAndPendingPayloadRoundTrip() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let disk = HubDisk(baseURL: URL(string: "https://example.invalid/hub/")!, root: root)
        let file = try disk.importFile(data: Data([0, 1, 2, 255]), name: "截图.png", mime: "image/png", existing: [])
        let draft = HubDraft(text: "继续修改", files: [file], agentIDs: [1], awaitingReceipt: true)
        try disk.save(draft, key: "user/1/pending/project/42")
        XCTAssertEqual(disk.load(HubDraft.self, key: "user/1/pending/project/42"), draft)
        XCTAssertNil(disk.load(HubDraft.self, key: "user/2/pending/project/42"))
        XCTAssertEqual(try Data(contentsOf: disk.fileURL(file.id)), Data([0, 1, 2, 255]))
        XCTAssertFalse(HubNativeRules.acceptsFiles(existing: [], newBytes: 51 * 1024 * 1024))
    }
    func testLongUnicodeDownloadNameFitsFilesystemAndPreservesExtension() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        for name in [String(repeating: "超长中文", count: 70) + ".png", String(repeating: "한글", count: 100) + ".pdf"] {
            let safe = HubPolicy.diskFilename(name)
            XCTAssertLessThanOrEqual(safe.decomposedStringWithCanonicalMapping.utf8.count, 240)
            XCTAssertEqual((safe as NSString).pathExtension, (name as NSString).pathExtension)
            let url = root.appendingPathComponent(safe)
            try Data([9,8,7]).write(to: url)
            XCTAssertEqual(try Data(contentsOf: url), Data([9,8,7]))
        }
    }
    @MainActor
    func testExpiredLoginKeepsDraftAndFileOnlyForVerifiedSameUser() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [HubNoNetworkProtocol.self]
        let model = HubSession(baseURL: URL(string: "https://example.invalid/hub/")!, connectAutomatically: false, configuration: config, storageRoot: root)
        try model.installFixture()
        model.draft.text = "A private unsent draft"
        model.addFile(data: Data([1, 2, 3]), name: "source.png", mime: "image/png")
        let key = model.draft.requestKey, file = try XCTUnwrap(model.draft.files.first)
        model.pause(); model.expireFixtureSession()
        XCTAssertNil(model.identity); XCTAssertTrue(model.draft.text.isEmpty)
        try model.installFixture(userID: 2, login: "other-user")
        XCTAssertTrue(model.draft.text.isEmpty); XCTAssertTrue(model.draft.files.isEmpty)
        model.expireFixtureSession(); try model.installFixture(userID: 1, login: "renewed-login")
        XCTAssertEqual(model.draft.text, "A private unsent draft")
        XCTAssertEqual(model.draft.requestKey, key)
        XCTAssertEqual(try Data(contentsOf: model.disk.fileURL(file.id)), Data([1, 2, 3]))
    }
    @MainActor
    func testInFlightMessageDoesNotLockOrClearTheNextDraft() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [HubNoNetworkProtocol.self]
        let model = HubSession(baseURL: URL(string: "https://example.invalid/hub/")!, connectAutomatically: false, configuration: config, storageRoot: root)
        try model.installFixture(); model.draft.text = "first message"; let original = model.draft.requestKey
        model.send(); model.draft.text = "second draft"; model.pause()
        XCTAssertEqual(model.pending?.requestKey, original); XCTAssertEqual(model.pending?.text, "first message")
        XCTAssertEqual(model.draft.text, "second draft"); XCTAssertFalse(model.sending)
        model.expireFixtureSession(); try model.installFixture(login: "renewed")
        XCTAssertEqual(model.pending?.requestKey, original)
        XCTAssertEqual(model.draft.text, "second draft")
    }
    func testCacheAndCallbacksRejectOtherSessionRoomAndGeneration() {
        let cached = HubCached(session: "one", savedAt: Date(), value: "private")
        XCTAssertNil(cached.read(session: "two"))
        XCTAssertNil(cached.read(session: "one", now: Date().addingTimeInterval(604900)))
        XCTAssertFalse(HubNativeRules.requestStillCurrent(generation: 2, expected: 1, session: "a", expectedSession: "a", scope: "p/1", expectedScope: "p/1"))
        XCTAssertFalse(HubNativeRules.requestStillCurrent(generation: 2, expected: 2, session: "b", expectedSession: "a", scope: "p/1", expectedScope: "p/1"))
        XCTAssertFalse(HubNativeRules.requestStillCurrent(generation: 2, expected: 2, session: "a", expectedSession: "a", scope: "p/2", expectedScope: "p/1"))
    }
}

private final class StubProtocol: URLProtocol {
    static var responseCode = 200
    static var responseHeaders = ["Content-Type": "application/json"]
    static var payload = Data()
    static var delay: TimeInterval = 0
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let status = Self.responseCode, headers = Self.responseHeaders, payload = Self.payload
        let block = { [weak self] in
            guard let self else { return }
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: payload)
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if Self.delay > 0 { DispatchQueue.global().asyncAfter(deadline: .now() + Self.delay, execute: block) }
        else { block() }
    }
    override func stopLoading() {}
}

final class HubTransportTests: XCTestCase {
    private func transport() -> HubTransport {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        return HubTransport(policy: HubPolicy(baseURL: URL(string: "https://example.invalid/hub/")!)!, configuration: config)
    }
    override func setUp() { StubProtocol.delay = 0; StubProtocol.responseCode = 200; StubProtocol.responseHeaders = ["Content-Type":"application/json"]; StubProtocol.payload = Data("{\"projects\":[]}".utf8) }
    func testNativeJSONAndWrongContentType() async throws {
        let client = transport()
        let value: HubProjectList = try await client.get("api/mobile/projects")
        XCTAssertTrue(value.projects.isEmpty)
        StubProtocol.responseHeaders = ["Content-Type":"text/html"]
        do { let _: HubProjectList = try await client.get("api/mobile/projects"); XCTFail("HTML is not a native page") } catch {}
    }
    func testRedirectNeverBecomesAWebPage() async {
        StubProtocol.responseCode = 302; StubProtocol.responseHeaders["Location"] = "https://example.invalid/hub/login"
        do { let _: HubProjectList = try await transport().get("api/mobile/projects"); XCTFail("Redirect must fail") } catch {}
    }
    func testStreamingDownloadRejectsLengthMismatchAndKeepsExactBytes() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let disk = HubDisk(baseURL: URL(string: "https://example.invalid/hub/")!, root: root)
        StubProtocol.responseHeaders = ["Content-Type":"image/png", "Content-Length":"4"]; StubProtocol.payload = Data([1,2,3,4])
        let good = HubAttachment(id: 1, name: "test.png", mimeType: "image/png", byteSize: 4, downloadUrl: "/hub/projects/demo/attachments/1", previewUrl: nil)
        let saved = try await transport().download(good, disk: disk)
        XCTAssertEqual(try Data(contentsOf: saved), Data([1,2,3,4]))
        let bad = HubAttachment(id: 2, name: "bad.png", mimeType: "image/png", byteSize: 5, downloadUrl: "/hub/projects/demo/attachments/2", previewUrl: nil)
        do { _ = try await transport().download(bad, disk: disk); XCTFail("Metadata mismatch must not save") } catch {}
    }
    func testCancelledAccountCannotCommitLateCookie() async throws {
        let url = URL(string: "https://example.invalid/hub/")!
        for cookie in HTTPCookieStorage.shared.cookies(for: url) ?? [] { HTTPCookieStorage.shared.deleteCookie(cookie) }
        StubProtocol.responseHeaders["Set-Cookie"] = "codex_hub_session=stale-user; Path=/hub; Secure"
        StubProtocol.delay = 0.2
        let client = transport()
        let request = Task { () -> HubProjectList in try await client.get("api/mobile/projects") }
        try await Task.sleep(for: .milliseconds(30)); client.cancelAll()
        _ = try? await request.value
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertFalse((HTTPCookieStorage.shared.cookies(for: url) ?? []).contains { $0.value == "stale-user" })
    }
    func testOversizedDeclaredJSONIsRejectedBeforeAccumulation() async {
        StubProtocol.responseHeaders["Content-Length"] = String(20 * 1024 * 1024)
        do { let _: HubProjectList = try await transport().get("api/mobile/projects"); XCTFail("Unbounded JSON") } catch {}
    }
}
