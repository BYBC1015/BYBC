import XCTest

final class HubNativeUITests: XCTestCase {
    private var app: XCUIApplication!
    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment["HUB_TESTING"] = "1"
        app.launchEnvironment["HUB_UI_TESTING"] = "1"
        app.launchEnvironment["HUB_RESET_FIXTURE"] = "1"
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["项目"].waitForExistence(timeout: 12))
    }
    override func tearDownWithError() throws { XCUIDevice.shared.orientation = .portrait; app.terminate() }
    private func capture(_ name: String) { let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot()); attachment.name = name; attachment.lifetime = .keepAlways; add(attachment) }
    private func waitForStableLandscape() {
        let deadline = Date().addingTimeInterval(12)
        var previous: [CGRect] = []
        var stableSince: Date?
        while Date() < deadline {
            let window = app.windows.firstMatch.frame
            let keyboard = app.keyboards.firstMatch.frame
            let composer = app.textFields["chat.composer"].frame
            let send = app.buttons["发送消息"].frame
            let frames = [window, keyboard, composer, send]
            let landscape = window.width > window.height && keyboard.width > keyboard.height
            let contained = window.contains(composer) && window.contains(send)
            let aboveKeyboard = composer.maxY <= keyboard.minY + 1 && send.maxY <= keyboard.minY + 1
            if landscape && contained && aboveKeyboard && frames == previous {
                if let since = stableSince, Date().timeIntervalSince(since) >= 1 { return }
                if stableSince == nil { stableSince = Date() }
            } else { stableSince = nil }
            previous = frames
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        capture("landscape-layout-not-stable")
        XCTFail("Landscape window, keyboard, composer and send frames did not settle: \(previous)")
    }

    func testNativeTabsDetailsAndLocalBackWithoutWebView() {
        XCTAssertEqual(app.webViews.count, 0)
        capture("native-chat-portrait")
        app.tabBars.buttons["项目"].tap()
        capture("native-project-list")
        app.buttons["project.workspace-team"].tap()
        XCTAssertTrue(app.textFields["chat.composer"].waitForExistence(timeout: 4))
        app.tabBars.buttons["智能体"].tap()
        capture("native-agent-list")
        app.buttons["agent.1"].tap()
        XCTAssertTrue(app.staticTexts["职责"].waitForExistence(timeout: 4))
        capture("native-agent-detail")
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["agent.1"].exists)
        app.tabBars.buttons["工作"].tap()
        app.buttons["task.3"].tap()
        XCTAssertTrue(app.navigationBars["工作详情"].waitForExistence(timeout: 4))
        capture("native-task-detail")
        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["工具箱"].tap()
        capture("native-toolbox-list")
        app.buttons["tool.devices"].tap()
        XCTAssertTrue(app.staticTexts["执行连接尚未就绪"].waitForExistence(timeout: 5))
        capture("native-offline-devices")
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["tool.feedback"].tap()
        XCTAssertTrue(app.textFields["简要说明"].waitForExistence(timeout: 4))
        XCTAssertEqual(app.webViews.count, 0)
    }
    func testOfflineDraftSurvivesTabsRelaunchAndLandscape() {
        let composer = app.textFields["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap(); composer.typeText("Offline draft 42")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        // Use the visible native control, never tap a tab through the keyboard.
        XCTAssertTrue(app.scrollViews["chat.messages"].exists)
        let dismiss = app.buttons["chat.dismiss-keyboard"]
        XCTAssertTrue(dismiss.waitForExistence(timeout: 5))
        XCTAssertTrue(dismiss.isHittable)
        dismiss.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["工具箱"].isHittable)
        app.tabBars.buttons["工具箱"].tap()
        XCTAssertTrue(app.buttons["tool.library"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["聊天"].isHittable)
        app.tabBars.buttons["聊天"].tap()
        XCTAssertEqual(app.textFields["chat.composer"].value as? String, "Offline draft 42")
        capture("native-offline-draft-portrait")
        app.textFields["chat.composer"].tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        capture("native-portrait-keyboard")
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(app.textFields["chat.composer"].waitForExistence(timeout: 5))
        waitForStableLandscape()
        XCTAssertTrue(app.keyboards.firstMatch.exists)
        XCTAssertTrue(app.textFields["chat.composer"].isHittable)
        XCTAssertTrue(app.buttons["发送消息"].isHittable)
        XCTAssertFalse(app.staticTexts["Siri, Dictation & Privacy"].exists)
        capture("native-offline-draft-landscape")
        app.terminate()
        app.launchEnvironment["HUB_RESET_FIXTURE"] = "0"
        app.launch()
        XCTAssertTrue(app.textFields["chat.composer"].waitForExistence(timeout: 8))
        XCTAssertEqual(app.textFields["chat.composer"].value as? String, "Offline draft 42")
        XCTAssertTrue(app.staticTexts["离线 · 可继续查看和写草稿"].exists)
        XCTAssertEqual(app.webViews.count, 0)
    }
    func testRoomChangePreservesSeparateDrafts() {
        app.textFields["chat.composer"].tap(); app.textFields["chat.composer"].typeText("Existing room draft")
        app.buttons["切换对话"].tap()
        app.buttons["发起新对话"].tap()
        app.textFields["chat.composer"].tap(); app.textFields["chat.composer"].typeText("New room draft")
        app.buttons["切换对话"].tap()
        app.buttons["room.1"].tap()
        XCTAssertEqual(app.textFields["chat.composer"].value as? String, "Existing room draft")
        app.buttons["切换对话"].tap(); app.buttons["发起新对话"].tap()
        XCTAssertEqual(app.textFields["chat.composer"].value as? String, "New room draft")
        capture("native-new-room-draft")
    }
}
