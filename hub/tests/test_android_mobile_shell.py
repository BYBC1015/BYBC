from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_ACTIVITY = (
    ROOT
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "xyz"
    / "baicang"
    / "codexhub"
    / "MainActivity.java"
)
MANIFEST = ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
NOTIFICATION_SERVICE = (
    ROOT
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "xyz"
    / "baicang"
    / "codexhub"
    / "HubNotificationService.java"
)
HUB_ENDPOINT = (
    ROOT
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "xyz"
    / "baicang"
    / "codexhub"
    / "HubEndpoint.java"
)
CSS = ROOT / "codex_hub" / "static" / "app.css"
VOICE_JS = ROOT / "codex_hub" / "static" / "hub-voice.js"
ANDROID_CSS = ROOT / "android" / "app" / "src" / "main" / "assets" / "android-app.css"
ANDROID_JS = ROOT / "android" / "app" / "src" / "main" / "assets" / "android-app.js"
PROJECT_DETAIL = ROOT / "codex_hub" / "templates" / "project_detail.html"
CHAT_ENTRY = ROOT / "codex_hub" / "templates" / "partials" / "project_chat_entry.html"
BUILD_GRADLE = ROOT / "android" / "app" / "build.gradle"
BUILD_SCRIPT = ROOT / "android" / "build-apk.ps1"


def test_android_webview_respects_system_bars_cutouts_and_keyboard():
    source = MAIN_ACTIVITY.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "setDecorFitsSystemWindows(false)" in source
    assert "View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN" in source
    assert "View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION" in source
    assert "FrameLayout rootView" in source
    assert "rootView.setOnApplyWindowInsetsListener" in source
    assert "WindowInsets.Type.systemBars()" in source
    assert "WindowInsets.Type.displayCutout()" in source
    assert "WindowInsets.Type.ime()" in source
    assert "Math.max(bars.bottom, ime.bottom)" in source
    assert "new WindowInsets.Builder(insets)" in source
    assert "android.graphics.Insets.NONE" in source
    assert "insets.getSystemWindowInsetTop()" in source
    assert "getSafeInsetTop()" in source
    assert "webView.setOnApplyWindowInsetsListener" not in source
    assert 'android:windowSoftInputMode="adjustResize"' in manifest


def test_android_webview_recovers_after_background_or_renderer_loss():
    source = MAIN_ACTIVITY.read_text(encoding="utf-8")

    assert "webView.restoreState(savedInstanceState) == null" in source
    assert "protected void onResume()" in source
    assert "webView.resumeTimers()" in source
    assert "scheduleResumeHealthCheck()" in source
    assert "postVisualStateCallback" in source
    assert "resume visual-state check timed out" in source
    assert "protected void onPause()" in source
    assert "webView.onPause()" in source
    assert "webView.pauseTimers()" in source
    assert "onRenderProcessGone" in source
    assert "rebuildPrimaryWebView(view)" in source
    assert "setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_BOUND, false)" in source
    assert "handleMainFrameLoadFailure" in source
    assert "scheduleMainFrameRetry" in source
    assert "onReceivedHttpError" in source
    assert "primaryPageReady" in source
    assert "primaryLoadInProgress" in source
    assert "primaryMainFrameFailed" in source
    assert "MAIN_FRAME_MAX_AUTO_RETRIES = 5" in source
    assert "registerDefaultNetworkCallback" in source
    assert "unregisterNetworkCallback" in source
    assert "NetworkCapabilities.NET_CAPABILITY_VALIDATED" in source
    assert "ACCESS_NETWORK_STATE" in MANIFEST.read_text(encoding="utf-8")


def test_android_defers_secondary_services_until_after_the_chat_starts():
    source = MAIN_ACTIVITY.read_text(encoding="utf-8")
    on_create = source[
        source.index("protected void onCreate(Bundle savedInstanceState)") :
        source.index("private WebView createPrimaryWebView()")
    ]

    assert "configureVoice();" not in on_create
    assert "requestNotificationPermission();" not in on_create
    assert "startBackgroundNotifications" not in on_create
    assert "scheduleAuthenticatedBackgroundWork()" in source
    assert "hasHubLoginCookies()" in source
    assert "rootView.postDelayed(this::startBackgroundNotifications, 1200L)" in source
    assert "requestNotificationPermission();" in source
    assert "pendingSpeechText" in source


def test_web_phone_chat_uses_most_of_dynamic_viewport_and_compact_composer():
    css = CSS.read_text(encoding="utf-8")

    assert "height: clamp(460px, 64vh, 720px)" in css
    assert "height: clamp(460px, 64dvh, 720px)" in css
    assert "grid-template-columns: minmax(0, 1fr) 40px 40px minmax(72px, auto)" in css
    assert "max-height: min(34svh, 320px)" not in css


def test_shared_chat_exposes_upload_and_direct_voice_controls():
    template = PROJECT_DETAIL.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    android_css = ANDROID_CSS.read_text(encoding="utf-8")
    voice_script = VOICE_JS.read_text(encoding="utf-8")
    chat_entry = CHAT_ENTRY.read_text(encoding="utf-8")

    assert "data-attachment-button" in template
    assert "data-attachment-input" in template
    assert 'data-speech-contract="voice-v1"' in template
    assert "data-voice-session" in template
    assert "data-voice-input" not in template
    assert "data-voice-speak-latest" not in template
    assert "data-composer-attachments" in template
    assert 'name="files" multiple' in template
    assert 'class="message-image"' in chat_entry
    assert ".workbench-chat .chat-message.from-user .avatar" in css
    assert "grid-template-columns: minmax(0, 1fr) 40px" in css
    assert "grid-template-columns: 70px 82px 42px 42px minmax(62px, 1fr)" in android_css
    assert "window.HubVoice" in voice_script
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in voice_script
    assert "send_requires_confirmation" not in voice_script
    assert "form.requestSubmit" not in voice_script


def test_phone_user_message_keeps_text_column_before_right_side_avatar():
    css = CSS.read_text(encoding="utf-8")

    mobile_user_rule = """.hub-detail-screen .workbench-chat .chat-message.from-user {
    grid-template-columns: minmax(0, 1fr) 34px;
  }"""
    assert mobile_user_rule in css


def test_apk_has_dedicated_fixed_chat_and_secondary_tools_modes():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    css = ANDROID_CSS.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")

    assert "codex-hub-android-mobile-layout" in android
    assert 'readTextAsset("android-app.css")' in android
    assert 'readTextAsset("android-app.js")' in android
    assert "android-chat-mode .workbench-chat" in css
    assert "flex: 1 1 auto" in css
    assert "android-chat-mode .workbench-composer" in css
    assert "flex: 0 0 auto" in css
    assert "android-tools-mode .android-tools-nav" in css
    assert "android-tool-section-active" in css
    assert 'body.classList.add("android-chat-mode")' in script
    assert 'body.classList.add("android-tools-mode")' in script
    assert "activateTool(targetId)" in script
    assert 'window.CodexHubAndroid' in script
    assert "android-project-drawer" in script
    assert "drawerBackdrop" not in script
    assert 'shell.append(projectDrawer)' in script
    assert 'drawerToggle.className = "android-drawer-toggle"' in script
    assert 'drawerToggle.setAttribute("aria-label", "打开项目侧边栏")' in script
    assert 'drawerToggle.setAttribute("aria-expanded", String(open))' in script
    assert 'touch.clientX > 44' in script
    assert "dx >= 64" in script
    assert "android-drawer-shortcuts" in script
    assert "新建项目" in script
    assert "管理项目成员" in script
    assert "设备、角色与群聊权限" in script
    assert 'newProjectUrl.hash = "new-project"' in script
    assert 'moreFeatures.textContent = "更多功能"' in script
    assert 'memoryButton.dataset.androidMemoryOpen = ""' in script
    assert 'showTools("control")' in script
    assert "projectDrawer.inert = !open" in script
    assert ".android-drawer-backdrop" not in css
    assert "button.scrollIntoView" not in script
    assert 'body.android-chat-mode .android-tools-back' in css
    assert 'body.android-tools-mode .android-tools-back' in css
    assert 'body.hub-detail-screen .memory-open-button' in css
    assert 'setHeaderButton("更多", false)' not in script
    assert (
        '".workbench-topbar > .icon-button:not(.mobile-rail-toggle)"' in script
    )
    assert 'shell.addEventListener("touchstart"' in script
    assert 'shell.addEventListener("touchmove"' not in script
    assert "touch-action: pan-y" in css
    assert "content-visibility: visible" in css
    assert "body.android-chat-mode .workbench-topbar {\n  display: none" not in css
    assert "flex-wrap: nowrap" in css
    assert "body.hub-detail-screen .android-drawer-toggle" in css
    assert "android-chat-mode.android-drawer-open .android-project-drawer" in css
    assert "transform: translateX(0)" in css
    assert "CodexHubAndroidVersion" in android
    assert "android-version-badge" in script
    assert "titleLine.insertBefore(badge, focusToggle || null)" in script
    assert "chat.scrollTop = chat.scrollHeight" not in script
    assert "chatWasNearBottom" in script
    assert "CodexHubReadingExperience?.pinToBottom?.()" in script
    assert '["媒体库", "files", "▧"]' in script
    assert "CodexHubMediaViewer" in script
    assert "CodexHubReadingExperience" in script
    assert "setMediaPlaybackRequiresUserGesture(true)" in android
    assert 'addJavascriptInterface(new HubMediaBridge(), "HubMedia")' in android
    assert "public void openImage(String url, String title)" in android
    assert "openImageViewer(url, title)" in android
    assert "public void haptic()" in android
    assert "HapticFeedbackConstants.KEYBOARD_TAP" in android
    assert "settings.setJavaScriptEnabled(false)" in android
    assert "settings.setAllowFileAccess(false)" in android
    assert "settings.setAllowContentAccess(true)" in android
    assert "WebSettings.MIXED_CONTENT_NEVER_ALLOW" in android
    assert 'path.contains("/attachments/")' in android
    assert "onConsoleMessage(ConsoleMessage consoleMessage)" in android
    assert 'typeof window.HubMedia?.openImage !== "function"' in script
    assert "window.CodexHubMediaViewer || window.CodexHubDeferredFeatures" in script
    assert "window.HubMedia.openImage" in script
    assert 'style.setProperty("display", "grid", "important")' in script
    assert 'style.setProperty("transform", "none", "important")' in script
    assert 'controlPane.inert = false' in script
    assert 'controlPane.setAttribute("aria-hidden", "false")' in script
    assert ".control-pane.android-tool-container-active" in css
    assert "position: static" in css
    assert "window.HubMedia?.haptic?.()" in script
    assert '["项目与设备", sidebarTargetId, "☷"]' in script
    assert "onPageCommitVisible" in android
    assert "injectAndroidCss(view)" in android
    assert 'settings.setCacheMode(WebSettings.LOAD_DEFAULT)' in android
    assert 'height:100%;background:#04070d' in android
    assert 'body{display:flex;align-items:center;justify-content:center' in android
    assert '" CodexHubAndroid/"' in android
    assert "setLayerType(View.LAYER_TYPE_HARDWARE" not in android
    assert 'android:hardwareAccelerated="true"' in MANIFEST.read_text(encoding="utf-8")


def test_apk_keeps_download_update_inside_the_existing_more_menu():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")
    css = ANDROID_CSS.read_text(encoding="utf-8")
    template = PROJECT_DETAIL.read_text(encoding="utf-8")
    gradle = BUILD_GRADLE.read_text(encoding="utf-8")
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert '["下载更新", ".workbench-topbar a[href*=\'/downloads\']", "↓"]' in script
    assert "button.dataset.androidUrl = link.href" in script
    assert "html.codex-hub-android body.android-tools-mode .android-tools-nav button" in css
    assert 'href="{{ app_path(\'/downloads\') }}"' in template
    assert "DOWNLOADS_URL" not in android
    assert "addDownloadUpdateButton" not in android
    assert "android-upgrade-fallback" not in template
    assert 'versionCode 27' in gradle
    assert 'versionName "0.1.26"' in gradle
    assert "Select-String -LiteralPath $GradleConfig" in build_script
    assert '[switch]$Clean' in build_script
    assert '@("clean", "assembleDebug")' in build_script
    assert '"dist\\CodexHub-v$VersionName-debug.apk"' in build_script
    assert "CodexHub-v0.1.9-debug.apk" not in build_script


def test_android_uses_private_local_state_and_native_picker_previews():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")
    template = PROJECT_DETAIL.read_text(encoding="utf-8")
    css = ANDROID_CSS.read_text(encoding="utf-8")

    assert 'LOCAL_DATA_PREFERENCES = "codex_hub_local_data"' in android
    assert 'addJavascriptInterface(new HubLocalDataBridge(), "HubLocalData")' in android
    assert "public String loadDraft(String key)" in android
    assert "public void saveDraft(String key, String value)" in android
    assert "LAST_INTERNAL_URL" in android
    assert "isRestorableProjectUrl" in android
    assert "Executors.newSingleThreadExecutor()" in android
    assert "scheduleLocalFilePreviews(result)" in android
    assert "getContentResolver().loadThumbnail" in android
    assert "LOCAL_PREVIEW_EDGE_PX = 320" in android
    assert "settings.setAllowFileAccess(false)" in android
    assert "settings.setAllowContentAccess(true)" in android
    assert "settings.setOffscreenPreRaster(true)" not in android

    assert "window.CodexHubAndroidFiles" in script
    assert "receiveSelection(files)" in script
    assert "previewFor(file)" in script
    assert "localPreviews.clear()" in script
    assert 'window.HubLocalData?.loadDraft' in script
    assert "window.HubLocalData.saveDraft" in script
    assert "window.HubLocalData.clearDraft" in script
    assert "window.setTimeout(saveLocalDraft, 500)" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert 'document.visibilityState === "hidden"' in script
    assert 'new CustomEvent("codexhub:native-file-previews")' in script
    assert 'item.dataset.fileKey = nativeFileKey(file)' in template
    assert 'window.CodexHubAndroidFiles?.previewFor?.(file)' in template
    assert 'window.addEventListener("codexhub:native-file-previews", renderAttachments)' in template
    assert 'window.CodexHubAndroidFiles?.clear?.()' in template
    assert ".composer-attachments:not([hidden])" in css


def test_android_back_button_returns_from_tools_to_chat_first():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")

    assert "window.CodexHubAndroid.back()" in android
    assert 'body.classList.contains("android-tools-mode")' in script
    assert "showChat();" in script
    assert "showChat(false)" not in script
    assert 'body.classList.contains("android-drawer-open")' in script


def test_android_shell_has_native_primary_navigation_and_accessible_drawer():
    script = ANDROID_JS.read_text(encoding="utf-8")
    css = ANDROID_CSS.read_text(encoding="utf-8")

    assert 'bottomNav.className = "android-bottom-nav"' in script
    assert '["chat", "●", "对话"]' in script
    assert '["projects", "▤", "项目"]' in script
    assert '["more", "•••", "更多"]' in script
    assert 'setPrimaryNav("chat")' in script
    assert "showTools(sidebarTargetId)" in script
    assert 'button.setAttribute("aria-current", "page")' in script
    assert 'drawerScrim.className = "android-drawer-scrim"' in script
    assert 'projectDrawer.querySelector("[data-android-drawer-close]")?.focus()' in script
    assert "lastDrawerFocus.focus({ preventScroll: true })" in script
    assert ".android-bottom-nav button" in css
    assert "min-height: 56px" in css
    assert ".android-drawer-scrim" in css
    assert "pointer-events: auto" in css


def test_android_shell_surfaces_send_and_network_state_without_losing_drafts():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")
    css = ANDROID_CSS.read_text(encoding="utf-8")

    assert 'connectionState.className = "android-connection-state"' in script
    assert 'window.addEventListener("offline"' in script
    assert 'window.addEventListener("online"' in script
    assert "当前离线，文字草稿已保存在本机" in script
    assert 'messageForm?.addEventListener("submit"' in script
    assert "event.stopImmediatePropagation()" in script
    assert "window.HubLocalData?.saveDraft?.(localDraftKey, composerInput.value)" in script
    assert "new MutationObserver(syncMessageFeedback)" in script
    assert "composerInput.style.height" in script
    assert ".composer-status.is-success:not(:empty)" in css
    assert ".composer-status.is-error:not(:empty)" in css
    assert "body.android-offline .workbench-composer" in css
    assert "primaryMainFrameFailed" in android
    assert "未发送的文字会安全保存在本机草稿中" in android
    assert 'retry.setText("重新连接")' in android
    assert "Gravity.CENTER" in android


def test_collaboration_notice_stays_out_of_chat_and_uses_mobile_overview():
    template = PROJECT_DETAIL.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")

    chat_start = template.index('<section id="conversation"')
    conversation_end = template.index("\n      </section>", chat_start)
    control_start = template.index('<section id="control"', conversation_end)
    collaboration_notice = template.index('class="collaboration-bar', control_start)

    assert chat_start < conversation_end < control_start < collaboration_notice
    assert '["概览", "control", "⌂"]' in script
    assert "android-chat-mode .control-pane" in ANDROID_CSS.read_text(encoding="utf-8")


def test_android_native_voice_uses_microphone_and_text_to_speech_bridge():
    android = MAIN_ACTIVITY.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "android.permission.RECORD_AUDIO" in manifest
    assert 'addJavascriptInterface(new HubVoiceBridge(), "HubVoice")' in android
    assert "SpeechRecognizer.createSpeechRecognizer" in android
    assert "RecognizerIntent.EXTRA_LANGUAGE" in android
    assert "new TextToSpeech" in android
    assert "TextToSpeech.QUEUE_FLUSH" in android
    assert "receiveTranscript" in android


def test_android_background_service_polls_session_endpoint_and_notifies_replies():
    service = NOTIFICATION_SERVICE.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "android.permission.POST_NOTIFICATIONS" in manifest
    assert "INITIAL_POLL_DELAY_SECONDS = 8L" in service
    assert "android.permission.FOREGROUND_SERVICE_REMOTE_MESSAGING" in manifest
    assert 'android:name=".HubNotificationService"' in manifest
    assert 'android:foregroundServiceType="remoteMessaging"' in manifest
    assert "HubEndpoint.current(this)" in service
    assert "CookieManager.getInstance().getCookie(hubUrl)" in service
    assert "PREF_SESSION_FINGERPRINT" in service
    assert "sha256(cookie.trim())" in service
    assert 'MessageDigest.getInstance("SHA-256")' in service
    assert 'api/mobile/notifications?after_entry_id=' in service
    assert "NetworkCapabilities.NET_CAPABILITY_VALIDATED" in service
    assert "MAX_RESPONSE_CHARS = 1_000_000" in service
    assert "readText(stream, MAX_RESPONSE_CHARS)" in service
    assert "connection.disconnect();" in service
    assert "startForeground(" in service
    assert "showReplyNotification(item)" in service
    assert "PendingIntent.FLAG_IMMUTABLE" in service
    assert 'MESSAGE_CHANNEL_ID = "hub-replies-with-sound-v2"' in service
    assert "NotificationManager.IMPORTANCE_HIGH" in service
    assert "RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)" in service
    assert "messageChannel.setSound(defaultNotificationSound(), soundAttributes)" in service
    assert "AudioAttributes.USAGE_NOTIFICATION_COMMUNICATION_INSTANT" in service
    assert "builder.setSound(defaultNotificationSound())" in service
    assert "serviceChannel.setSound(null, null)" in service
    assert "Unable to poll Hub for mobile notifications" in service
    assert "未开启通知权限，新消息不会响铃" in MAIN_ACTIVITY.read_text(encoding="utf-8")
    assert "Bearer" not in service


def test_android_handles_secondary_renderer_loss_and_cancels_pending_picker():
    source = MAIN_ACTIVITY.read_text(encoding="utf-8")

    assert '"Image viewer renderer exited; closing preview. crashed="' in source
    assert "if (imageViewerWebView == view) imageViewerWebView = null;" in source
    assert "crashedDialog.dismiss();" in source
    assert "pendingFileCallback.onReceiveValue(null);" in source
    assert 'webView.loadUrl("about:blank")' not in source


def test_android_server_endpoint_can_move_without_rebuilding_the_apk():
    activity = MAIN_ACTIVITY.read_text(encoding="utf-8")
    endpoint = HUB_ENDPOINT.read_text(encoding="utf-8")
    service = NOTIFICATION_SERVICE.read_text(encoding="utf-8")
    script = ANDROID_JS.read_text(encoding="utf-8")

    assert 'HUB_BASE_URL = "hub_base_url"' in endpoint
    assert '"https".equalsIgnoreCase(uri.getScheme())' in endpoint
    assert "uri.getUserInfo() != null" in endpoint
    assert "uri.getPort() > 65535" in endpoint
    assert "uri.getQuery() != null" in endpoint
    assert "uri.getFragment() != null" in endpoint
    assert ".remove(LAST_INTERNAL_URL)" in endpoint
    assert ".remove(NOTIFICATION_LAST_ENTRY_ID)" in endpoint
    assert "effectivePort(configured) != effectivePort(candidate)" in endpoint
    assert "candidate.getUserInfo() != null" in endpoint
    assert "candidatePath.startsWith(basePath)" in endpoint
    assert "HubEndpoint.projectUrl(this, projectSlug)" in service

    assert 'addJavascriptInterface(new HubAppBridge(), "HubApp")' in activity
    assert "showServerSettings()" in activity
    assert 'server.setText("服务器")' in activity
    assert "HubEndpoint.normalize(rawValue)" in activity
    assert 'showConnectionRecovery("正在连接新服务器…")' in activity
    assert "return HubEndpoint.current(this);" in activity
    assert "return HubEndpoint.isInternal(this, uri);" in activity
    assert "loadDataWithBaseURL(HubEndpoint.current(this)" in activity

    assert "data-android-server-settings" in script
    assert "window.HubApp?.openServerSettings?.()" in script
