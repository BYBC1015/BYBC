import XCTest
@testable import Hub

final class HubPolicyTests: XCTestCase {
    let policy = HubPolicy(baseURL: URL(string: "https://example.invalid/hub/")!)!
    func url(_ path: String) -> URL { URL(string: path, relativeTo: policy.baseURL)!.absoluteURL }

    func testCurrentConversationAndProjectCanRestore() {
        XCTAssertTrue(policy.isWorkspace(url("projects/demo/conversations/31?task_history=1")))
        XCTAssertTrue(policy.isWorkspace(url("projects/demo")))
        XCTAssertFalse(policy.isWorkspace(url("api/projects/demo")))
        XCTAssertFalse(policy.isWorkspace(url("projects/demo/conversations/no-id")))
        XCTAssertFalse(policy.isWorkspace(url("projects/demo/attachments/5")))
    }
    func testTrustBoundaryRejectsOtherHostsPortsCredentialsAndTraversal() {
        for address in ["https://evil.invalid/hub/projects/demo", "https://example.invalid:444/hub/projects/demo",
                        "https://user@example.invalid/hub/projects/demo", "http://example.invalid/hub/projects/demo",
                        "https://example.invalid/hub/projects/%2e%2e/private", "https://example.invalid/hub2/projects/demo"] {
            XCTAssertFalse(policy.isInternal(URL(string: address)!), address)
        }
        XCTAssertTrue(policy.isInternal(URL(string: "https://example.invalid:443/hub/projects/demo")!))
    }
    func testDownloadCannotBecomeRecoveryOrLogin() {
        XCTAssertTrue(policy.isDownload(url("projects/demo/attachments/9")))
        XCTAssertTrue(policy.isDownload(url("downloads/android")))
        XCTAssertFalse(policy.isDownload(url("login")))
        XCTAssertFalse(policy.canRecover(url("projects/demo/attachments/9")))
        XCTAssertFalse(policy.canRecover(url("downloads/android")))
        XCTAssertFalse(policy.canRecover(url("api/projects/demo")))
        XCTAssertTrue(policy.canRecover(url("projects/demo/attachments/9/preview")))
        XCTAssertTrue(policy.canRecover(url("login")))
    }
    func testFilenameCannotEscapePrivateDownloadDirectory() {
        XCTAssertEqual(HubPolicy.safeFilename("../图\\片\n.png"), ".._图_片_.png")
        XCTAssertEqual(HubPolicy.safeFilename(".."), "Hub 文件")
        XCTAssertEqual(HubPolicy.safeFilename(" 成果.png "), "成果.png")
        XCTAssertEqual(HubPolicy.safeFilename(String(repeating: "字", count: 180)).count, 160)
    }

    func testRecoveryRequestsFreshHTMLWithoutReplayingForms() {
        let target = url("projects/demo/conversations/31")
        let request = policy.pageRequest(target)
        XCTAssertEqual(request.url, target)
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertNil(request.httpBody)
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-cache")
        XCTAssertEqual(request.timeoutInterval, 30)
    }

    func testMaintenancePageKeepsItsOwnRouteWithoutConversationRestoration() {
        let archive = url("projects/demo?view=archive&tab=assets")
        XCTAssertFalse(policy.isWorkspace(archive))
        XCTAssertTrue(policy.canRecover(archive))
        XCTAssertEqual(policy.pageRequest(archive).url, archive)
        XCTAssertTrue(policy.isAuthentication(url("login")))
        XCTAssertTrue(policy.isAuthentication(url("logout")))
        XCTAssertFalse(policy.isAuthentication(url("projects/login")))
    }

    func testNativeReadOnlyRecordBindsSessionProjectRoomAndAge() {
        let now = Date(timeIntervalSince1970: 1000000)
        let location = url("projects/demo/conversations/31")
        let identity = HubLocalRecord.sessionDigest("test-session-only")
        let record = HubLocalRecord(url: location, session: identity, user: "2", project: "demo", conversation: "31",
            title: "当前对话", messages: [HubLocalMessage(author: "你", text: "已发送的消息")], savedAt: now)
        XCTAssertTrue(record.readable(policy: policy, location: location, session: identity, now: now))
        XCTAssertFalse(record.readable(policy: policy, location: location, session: "", now: now))
        XCTAssertFalse(record.readable(policy: policy, location: location, session: HubLocalRecord.sessionDigest("other-account"), now: now))
        XCTAssertFalse(record.readable(policy: policy, location: url("projects/demo/conversations/32"), session: identity, now: now))
        XCTAssertFalse(record.readable(policy: policy, location: url("projects/other/conversations/31"), session: identity, now: now))
        XCTAssertFalse(record.readable(policy: policy, location: location, session: identity, now: now.addingTimeInterval(604801)))
        XCTAssertFalse(record.readable(policy: policy, location: location, session: identity, now: now.addingTimeInterval(-1)))
        XCTAssertEqual(identity.count, 64)
        XCTAssertFalse(identity.contains("test-session"))
        XCTAssertEqual(HubLocalRecord.sessionDigest(""), "")
    }

    func testSnapshotWithWrongDOMIdentityCannotBeReadOrSaved() {
        let now = Date(), location = url("projects/demo/conversations/31")
        for (user, project, conversation) in [("", "demo", "31"), ("2", "wrong", "31"), ("2", "demo", "32")] {
            let record = HubLocalRecord(url: location, session: "digest", user: user, project: project, conversation: conversation,
                title: "Wrong scope", messages: [], savedAt: now)
            XCTAssertFalse(record.readable(policy: policy, location: location, session: "digest", now: now))
        }
    }

    func testOldUIReleaseAndDeepLinkCacheCannotMasqueradeAsCurrent() {
        XCTAssertTrue(HubPolicy.currentRelease("2.11.0"))
        XCTAssertTrue(HubPolicy.currentRelease("3.0.0"))
        XCTAssertFalse(HubPolicy.currentRelease("2.10.99"))
        XCTAssertFalse(HubPolicy.currentRelease(""))
        XCTAssertTrue(policy.sameDocument(url("projects/demo"), url("projects/demo#work-entry-7")))
        XCTAssertFalse(policy.sameDocument(url("projects/demo"), url("projects/demo/conversations/31#work-entry-7")))
    }
}
