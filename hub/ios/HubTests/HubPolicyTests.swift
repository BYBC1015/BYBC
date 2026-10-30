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
}
