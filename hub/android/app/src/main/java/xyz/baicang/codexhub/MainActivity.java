package xyz.baicang.codexhub;

import android.Manifest;
import android.annotation.SuppressLint;
import android.annotation.TargetApi;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Context;
import android.content.ContentValues;
import android.app.Dialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.util.Base64;
import android.util.Log;
import android.util.Size;
import android.view.View;
import android.view.ViewGroup;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String TAG = "CodexHub";
    private static final int FILE_CHOOSER_REQUEST = 1001;
    private static final int RECORD_AUDIO_PERMISSION_REQUEST = 1002;
    private static final int NOTIFICATION_PERMISSION_REQUEST = 1003;
    private static final int SAVE_DOWNLOAD_REQUEST = 1004;
    private static final String MOBILE_LAYOUT_STYLE_ID = "codex-hub-android-mobile-layout";
    private static final String LOCAL_DATA_PREFERENCES = "codex_hub_local_data";
    private static final String LAST_INTERNAL_URL = "last_internal_url";
    private static final int LOCAL_PREVIEW_EDGE_PX = 320;
    private static final int MAX_LOCAL_PREVIEWS = 12;
    private static final long MAIN_FRAME_RETRY_BASE_MS = 1000L;
    private static final long MAIN_FRAME_RETRY_MAX_MS = 15000L;
    private static final int MAIN_FRAME_MAX_AUTO_RETRIES = 5;
    private FrameLayout rootView;
    private WebView webView;
    private LinearLayout connectionRecoveryView;
    private TextView connectionRecoveryTitle;
    private TextView connectionRecoveryStatus;
    private ValueCallback<Uri[]> pendingFileCallback;
    private String pendingFilePage = "";
    private long filePageGeneration = 0L;
    private long pendingFileGeneration = 0L;
    private SharedAttachmentImporter sharedAttachmentImporter;
    private final ArrayList<Uri> pendingSharedFiles = new ArrayList<>();
    private final ArrayList<Uri> importedSharedFiles = new ArrayList<>();
    private boolean sharedImportInProgress = false;
    private boolean activityDestroyed = false;
    private long sharedImportGeneration = 0L;
    private String androidAppCss = "";
    private String androidAppScript = "";
    private String androidWorkspaceScript = "";
    private String androidRecoveryScript = "";
    private TextToSpeech textToSpeech;
    private boolean textToSpeechReady = false;
    private String pendingSpeechText = "";
    private SpeechRecognizer speechRecognizer;
    private boolean startListeningAfterPermission = false;
    private Dialog imageViewerDialog;
    private WebView imageViewerWebView;
    private SharedPreferences localData;
    private final ExecutorService localDataExecutor = Executors.newSingleThreadExecutor();
    private final ExecutorService downloadExecutor = Executors.newSingleThreadExecutor();
    private final Set<String> activeDownloads = new LinkedHashSet<>();
    private HubFileDownload.Result pendingDownload;
    private long resumeProbeGeneration = 0L;
    private long completedResumeProbeGeneration = 0L;
    private boolean activityResumed = false;
    private boolean backgroundNotificationsStarted = false;
    private boolean backgroundNotificationsScheduled = false;
    private boolean notificationPermissionRequested = false;
    private int mainFrameFailureCount = 0;
    private long mainFrameRetryGeneration = 0L;
    private boolean primaryPageReady = false;
    private boolean primaryLoadInProgress = false;
    private boolean primaryMainFrameFailed = false;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean networkCallbackRegistered = false;
    private String lastCommittedInternalUrl = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        androidAppCss = readTextAsset("android-app.css");
        androidAppScript = readTextAsset("android-app.js");
        androidWorkspaceScript = readTextAsset("android-workspace.js");
        androidRecoveryScript = readTextAsset("android-recovery.js");
        localData = getSharedPreferences(LOCAL_DATA_PREFERENCES, Context.MODE_PRIVATE);
        sharedAttachmentImporter = new SharedAttachmentImporter(this);
        restoreSharedFiles(savedInstanceState);
        lastCommittedInternalUrl = HubEndpoint.current(this);
        connectivityManager = (ConnectivityManager) getSystemService(
                Context.CONNECTIVITY_SERVICE
        );

        rootView = new FrameLayout(this);
        rootView.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
        rootView.setBackgroundColor(Color.rgb(255, 254, 250));

        webView = createPrimaryWebView();
        rootView.addView(webView);
        connectionRecoveryView = createConnectionRecoveryView();
        rootView.addView(connectionRecoveryView);
        setContentView(rootView);
        configureWindowInsets();
        configureWebView();

        String launchUrl = initialUrl(getIntent());
        lastCommittedInternalUrl = launchUrl;
        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            loadPrimaryUrl(webView, launchUrl);
        }
        if (savedInstanceState == null) receiveSharedFiles(getIntent());
    }

    private WebView createPrimaryWebView() {
        WebView view = new WebView(this);
        view.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
        view.setBackgroundColor(Color.rgb(255, 254, 250));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            view.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_BOUND, false);
        }
        return view;
    }

    private LinearLayout createConnectionRecoveryView() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER_HORIZONTAL);
        panel.setPadding(dp(24), dp(22), dp(24), dp(20));
        panel.setFocusable(true);
        panel.setClickable(true);
        panel.setContentDescription("连接恢复面板");
        GradientDrawable card = new GradientDrawable();
        card.setColor(Color.rgb(255, 254, 250));
        card.setCornerRadius(dp(18));
        card.setStroke(dp(1), Color.rgb(221, 226, 209));
        panel.setBackground(card);
        panel.setVisibility(View.GONE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            panel.setElevation(dp(8));
        }

        TextView recoveryIcon = new TextView(this);
        recoveryIcon.setText("↻");
        recoveryIcon.setGravity(Gravity.CENTER);
        recoveryIcon.setTextColor(Color.rgb(116, 99, 34));
        recoveryIcon.setTextSize(28);
        panel.addView(recoveryIcon, new LinearLayout.LayoutParams(dp(48), dp(48)));

        connectionRecoveryTitle = new TextView(this);
        connectionRecoveryTitle.setText("正在恢复连接");
        connectionRecoveryTitle.setGravity(Gravity.CENTER);
        connectionRecoveryTitle.setTextColor(Color.rgb(38, 48, 32));
        connectionRecoveryTitle.setTextSize(20);
        connectionRecoveryTitle.setTypeface(
                connectionRecoveryTitle.getTypeface(),
                android.graphics.Typeface.BOLD
        );
        LinearLayout.LayoutParams titleLayout = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        titleLayout.setMargins(0, dp(6), 0, dp(6));
        panel.addView(connectionRecoveryTitle, titleLayout);

        connectionRecoveryStatus = new TextView(this);
        connectionRecoveryStatus.setGravity(Gravity.CENTER);
        connectionRecoveryStatus.setTextColor(Color.rgb(103, 114, 94));
        connectionRecoveryStatus.setTextSize(15);
        connectionRecoveryStatus.setLineSpacing(0f, 1.15f);
        panel.addView(connectionRecoveryStatus, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        TextView recoveryHint = new TextView(this);
        recoveryHint.setText("未发送的文字会安全保存在本机草稿中");
        recoveryHint.setGravity(Gravity.CENTER);
        recoveryHint.setTextColor(Color.rgb(103, 114, 94));
        recoveryHint.setTextSize(13);
        LinearLayout.LayoutParams hintLayout = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        hintLayout.setMargins(0, dp(8), 0, dp(16));
        panel.addView(recoveryHint, hintLayout);

        Button retry = new Button(this);
        retry.setText("重新连接");
        retry.setTextSize(15);
        retry.setAllCaps(false);
        retry.setMinHeight(dp(48));
        retry.setBackgroundTintList(android.content.res.ColorStateList.valueOf(
                Color.rgb(243, 214, 80)
        ));
        retry.setTextColor(Color.rgb(53, 59, 41));
        retry.setOnClickListener(ignored -> {
            retry.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP);
            retryPrimaryWebViewNow();
        });
        panel.addView(retry, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(50)
        ));

        Button server = new Button(this);
        server.setText("服务器");
        server.setTextSize(15);
        server.setAllCaps(false);
        server.setMinHeight(dp(48));
        server.setBackgroundTintList(android.content.res.ColorStateList.valueOf(
                Color.rgb(241, 243, 233)
        ));
        server.setTextColor(Color.rgb(72, 84, 62));
        server.setOnClickListener(ignored -> showServerSettings());
        LinearLayout.LayoutParams serverLayout = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(50)
        );
        serverLayout.setMargins(0, dp(8), 0, 0);
        panel.addView(server, serverLayout);

        FrameLayout.LayoutParams layout = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER
        );
        layout.setMargins(dp(24), dp(24), dp(24), dp(24));
        panel.setLayoutParams(layout);
        return panel;
    }

    @Override
    protected void onStart() {
        super.onStart();
        HubNotificationService.setAppInForeground(true);
        registerNetworkCallback();
    }

    @Override
    protected void onResume() {
        super.onResume();
        activityResumed = true;
        if (webView != null) {
            webView.onResume();
            webView.resumeTimers();
            webView.setVisibility(View.VISIBLE);
            webView.invalidate();
            scheduleResumeHealthCheck();
            notifyWorkspaceNetwork();
            if (mainFrameFailureCount > 0) scheduleMainFrameRetry();
        }
        if (primaryPageReady) {
            scheduleAuthenticatedBackgroundWork();
        }
    }

    @Override
    protected void onPause() {
        activityResumed = false;
        resumeProbeGeneration += 1L;
        if (webView != null) {
            webView.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onStop() {
        HubNotificationService.setAppInForeground(false);
        unregisterNetworkCallback();
        if (webView != null) {
            webView.pauseTimers();
        }
        super.onStop();
    }

    private void scheduleResumeHealthCheck() {
        WebView target = webView;
        if (target == null || target.getUrl() == null
                || !primaryPageReady || primaryLoadInProgress) return;
        long generation = ++resumeProbeGeneration;
        target.postDelayed(() -> {
            if (!activityResumed || target != webView || generation != resumeProbeGeneration) {
                return;
            }
            target.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
                @Override
                public void onComplete(long requestId) {
                    if (!activityResumed || target != webView
                            || generation != resumeProbeGeneration) return;
                    completedResumeProbeGeneration = generation;
                    target.evaluateJavascript(
                            "(function(){return !!(document.body&&document.body.childElementCount);})()",
                            result -> {
                                if (!activityResumed || target != webView
                                        || generation != resumeProbeGeneration) return;
                                if (!"true".equals(result)) {
                                    reloadPrimaryWebView("resume DOM health check failed");
                                    return;
                                }
                                target.setVisibility(View.VISIBLE);
                                target.invalidate();
                                target.requestFocus();
                            }
                    );
                }
            });
            target.postDelayed(() -> {
                if (activityResumed && target == webView
                        && generation == resumeProbeGeneration
                        && completedResumeProbeGeneration < generation) {
                    reloadPrimaryWebView("resume visual-state check timed out");
                }
            }, 5000L);
        }, 250L);
    }

    private void handleMainFrameLoadFailure(String detail) {
        primaryLoadInProgress = false;
        primaryPageReady = false;
        primaryMainFrameFailed = true;
        mainFrameFailureCount = Math.min(mainFrameFailureCount + 1, 8);
        showConnectionRecovery(
                hasUsableNetwork()
                        ? "连接暂时中断，正在自动恢复"
                        : "当前网络不可用，联网后将自动恢复"
        );
        Log.w(TAG, "Main WebView navigation failed: " + detail);
        scheduleMainFrameRetry();
    }

    private void scheduleMainFrameRetry() {
        WebView target = webView;
        if (target == null || !activityResumed || primaryLoadInProgress) return;
        if (mainFrameFailureCount >= MAIN_FRAME_MAX_AUTO_RETRIES) {
            showConnectionRecovery("自动恢复已暂停，请点按重试再次尝试");
            return;
        }
        if (!hasUsableNetwork()) {
            showConnectionRecovery("当前网络不可用，联网后将自动恢复");
            return;
        }
        long generation = ++mainFrameRetryGeneration;
        int exponent = Math.min(Math.max(mainFrameFailureCount - 1, 0), 4);
        long delay = Math.min(
                MAIN_FRAME_RETRY_MAX_MS,
                MAIN_FRAME_RETRY_BASE_MS * (1L << exponent)
        );
        target.postDelayed(() -> {
            if (target != webView || generation != mainFrameRetryGeneration) return;
            if (!activityResumed || primaryLoadInProgress) return;
            if (!hasUsableNetwork()) {
                showConnectionRecovery("当前网络不可用，联网后将自动恢复");
                return;
            }
            showConnectionRecovery("正在重新连接…");
            loadPrimaryUrl(target, recoveryUrl(target));
        }, delay);
    }

    private void retryPrimaryWebViewNow() {
        WebView target = webView;
        if (target == null || primaryLoadInProgress) return;
        mainFrameRetryGeneration += 1L;
        if (!hasUsableNetwork()) {
            showConnectionRecovery("当前网络不可用，请检查网络后重试");
            return;
        }
        mainFrameFailureCount = Math.min(
                mainFrameFailureCount,
                MAIN_FRAME_MAX_AUTO_RETRIES - 1
        );
        showConnectionRecovery("正在重新连接…");
        loadPrimaryUrl(target, recoveryUrl(target));
    }

    private boolean hasUsableNetwork() {
        ConnectivityManager manager = connectivityManager;
        if (manager == null) return true;
        Network active = manager.getActiveNetwork();
        NetworkCapabilities capabilities = manager.getNetworkCapabilities(active);
        return capabilities != null
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
    }

    private void registerNetworkCallback() {
        if (connectivityManager == null || networkCallbackRegistered) return;
        if (networkCallback == null) {
            networkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    handleNetworkStateChanged();
                }

                @Override
                public void onCapabilitiesChanged(
                        Network network,
                        NetworkCapabilities networkCapabilities
                ) {
                    handleNetworkStateChanged();
                }

                @Override
                public void onLost(Network network) {
                    handleNetworkStateChanged();
                }
            };
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                connectivityManager.registerDefaultNetworkCallback(networkCallback);
            } else {
                NetworkRequest request = new NetworkRequest.Builder()
                        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                        .build();
                connectivityManager.registerNetworkCallback(request, networkCallback);
            }
            networkCallbackRegistered = true;
        } catch (RuntimeException exception) {
            Log.w(TAG, "Unable to observe network recovery", exception);
        }
    }

    private void unregisterNetworkCallback() {
        if (connectivityManager == null || networkCallback == null
                || !networkCallbackRegistered) return;
        try {
            connectivityManager.unregisterNetworkCallback(networkCallback);
        } catch (RuntimeException exception) {
            Log.w(TAG, "Unable to stop observing network recovery", exception);
        } finally {
            networkCallbackRegistered = false;
        }
    }

    private void handleNetworkStateChanged() {
        runOnUiThread(() -> {
            if (!activityResumed) return;
            notifyWorkspaceNetwork();
            if (mainFrameFailureCount <= 0) return;
            if (!hasUsableNetwork()) {
                showConnectionRecovery("当前网络不可用，联网后将自动恢复");
                return;
            }
            retryPrimaryWebViewNow();
        });
    }

    private void notifyWorkspaceNetwork() {
        WebView target = webView;
        if (target == null || !primaryPageReady || primaryLoadInProgress
                || target.getUrl() == null || !isInternalUrl(Uri.parse(target.getUrl()))) return;
        target.evaluateJavascript("window.CodexHubNativeRecovery&&window.CodexHubNativeRecovery.wake("
                + hasUsableNetwork() + ");", null);
    }

    private void showConnectionRecovery(String message) {
        if (connectionRecoveryView == null || connectionRecoveryStatus == null
                || connectionRecoveryTitle == null) return;
        boolean wasHidden = connectionRecoveryView.getVisibility() != View.VISIBLE;
        connectionRecoveryTitle.setText(
                hasUsableNetwork() ? "正在恢复连接" : "等待网络连接"
        );
        connectionRecoveryStatus.setText(message);
        connectionRecoveryView.setVisibility(View.VISIBLE);
        if (wasHidden) {
            connectionRecoveryView.requestFocus();
            connectionRecoveryView.announceForAccessibility(
                    connectionRecoveryTitle.getText() + "。" + message
            );
        }
    }

    private void markPrimaryPageVisible() {
        // Content-addressed assets can use their normal cache after fresh HTML commits.
        if (webView != null) webView.getSettings().setCacheMode(WebSettings.LOAD_DEFAULT);
        primaryLoadInProgress = false;
        primaryPageReady = true;
        primaryMainFrameFailed = false;
        mainFrameFailureCount = 0;
        mainFrameRetryGeneration += 1L;
        if (connectionRecoveryView != null) {
            connectionRecoveryView.setVisibility(View.GONE);
        }
        scheduleAuthenticatedBackgroundWork();
    }

    private void reloadPrimaryWebView(String reason) {
        WebView target = webView;
        if (target == null) return;
        String url = recoveryUrl(target);
        Log.w(TAG, "Recovering blank WebView: " + reason + " url=" + url);
        resumeProbeGeneration += 1L;
        showConnectionRecovery("页面正在恢复…");
        loadPrimaryUrl(target, url);
    }

    private String recoveryUrl(WebView target) {
        String currentUrl = target == null ? "" : String.valueOf(target.getUrl());
        if (!currentUrl.isEmpty() && !"null".equals(currentUrl)) {
            Uri current = Uri.parse(currentUrl);
            if (HubNavigationPolicy.recoverable(HubEndpoint.current(this), currentUrl)) {
                return currentUrl;
            }
        }
        Uri committed = Uri.parse(lastCommittedInternalUrl);
        if (HubNavigationPolicy.recoverable(HubEndpoint.current(this), lastCommittedInternalUrl)) {
            return lastCommittedInternalUrl;
        }
        return initialUrl(getIntent());
    }

    private void rebuildPrimaryWebView(WebView crashedView) {
        String url = recoveryUrl(crashedView);
        resumeProbeGeneration += 1L;
        mainFrameFailureCount = Math.min(mainFrameFailureCount + 1, 8);
        mainFrameRetryGeneration += 1L;
        showConnectionRecovery("页面进程已恢复，正在重新连接…");
        if (pendingFileCallback != null) {
            pendingFileCallback.onReceiveValue(null);
            pendingFileCallback = null;
        }
        if (rootView != null && crashedView != null) {
            rootView.removeView(crashedView);
        }
        if (crashedView != null) {
            crashedView.removeJavascriptInterface("HubVoice");
            crashedView.removeJavascriptInterface("HubMedia");
            crashedView.removeJavascriptInterface("HubLocalData");
            crashedView.removeJavascriptInterface("HubApp");
            crashedView.destroy();
        }
        webView = createPrimaryWebView();
        configureWebView();
        rootView.addView(webView, 0);
        loadPrimaryUrl(webView, url);
    }

    private void loadPrimaryUrl(WebView target, String url) {
        if (target == null || target != webView) return;
        primaryPageReady = false;
        primaryLoadInProgress = true;
        primaryMainFrameFailed = false;
        resumeProbeGeneration += 1L;
        // Recover GET from the network; never clear cookies, IndexedDB or file imports.
        target.getSettings().setCacheMode(WebSettings.LOAD_NO_CACHE);
        target.loadUrl(url);
    }

    private void configureWindowInsets() {
        int lightBars = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            lightBars |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            lightBars |= View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
        }
        getWindow().getDecorView().setSystemUiVisibility(lightBars);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            getWindow().setStatusBarColor(Color.TRANSPARENT);
            getWindow().setNavigationBarColor(Color.TRANSPARENT);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            getWindow().setNavigationBarContrastEnforced(false);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
        }

        rootView.setOnApplyWindowInsetsListener((view, insets) -> {
            int left;
            int top;
            int right;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets bars = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                );
                android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
                left = bars.left;
                top = bars.top;
                right = bars.right;
                bottom = Math.max(bars.bottom, ime.bottom);
                view.setPadding(left, top, right, bottom);
                return new WindowInsets.Builder(insets)
                        .setInsets(
                                WindowInsets.Type.systemBars()
                                        | WindowInsets.Type.displayCutout()
                                        | WindowInsets.Type.ime(),
                                android.graphics.Insets.NONE
                        )
                        .build();
            }

            left = insets.getSystemWindowInsetLeft();
            top = insets.getSystemWindowInsetTop();
            right = insets.getSystemWindowInsetRight();
            bottom = insets.getSystemWindowInsetBottom();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && insets.getDisplayCutout() != null) {
                left = Math.max(left, insets.getDisplayCutout().getSafeInsetLeft());
                top = Math.max(top, insets.getDisplayCutout().getSafeInsetTop());
                right = Math.max(right, insets.getDisplayCutout().getSafeInsetRight());
                bottom = Math.max(bottom, insets.getDisplayCutout().getSafeInsetBottom());
            }
            view.setPadding(left, top, right, bottom);
            WindowInsets cleared = insets.consumeSystemWindowInsets();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && cleared.getDisplayCutout() != null) {
                cleared = cleared.consumeDisplayCutout();
            }
            return cleared;
        });
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH) {
            rootView.post(() -> rootView.requestApplyInsets());
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, false);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setUserAgentString(
                settings.getUserAgentString()
                        + " CodexHubAndroid/"
                        + BuildConfig.VERSION_NAME
        );
        settings.setAllowFileAccess(false);
        // Android's system picker returns content:// URIs. WebView needs content access to
        // turn the explicitly selected URI grants into File objects for preview and upload.
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setGeolocationEnabled(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        webView.addJavascriptInterface(new HubVoiceBridge(), "HubVoice");
        webView.addJavascriptInterface(new HubMediaBridge(), "HubMedia");
        webView.addJavascriptInterface(new HubLocalDataBridge(), "HubLocalData");
        webView.addJavascriptInterface(new HubAppBridge(), "HubApp");
        webView.setWebViewClient(new HubWebViewClient());
        webView.setWebChromeClient(new HubWebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
                startHubDownload(url, userAgent));
        webView.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) {
            webView.saveState(outState);
        }
        ArrayList<String> imported = new ArrayList<>();
        for (Uri uri : importedSharedFiles) imported.add(uri.toString());
        ArrayList<String> pending = new ArrayList<>();
        for (Uri uri : pendingSharedFiles) pending.add(uri.toString());
        outState.putStringArrayList("imported_shared_files", imported);
        outState.putStringArrayList("pending_shared_files", pending);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        // A share resumes this same Activity and its existing WebView/draft.
        if (receiveSharedFiles(intent)) return;
        setIntent(intent);
        String url = initialUrl(intent);
        if (webView != null && isInternalUrl(Uri.parse(url))) {
            loadPrimaryUrl(webView, url);
        }
    }

    @Override
    public void onBackPressed() {
        if (imageViewerDialog != null && imageViewerDialog.isShowing()) {
            imageViewerDialog.dismiss();
            return;
        }
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        webView.evaluateJavascript(
                "(function(){return !!(window.CodexHubAndroid && window.CodexHubAndroid.back());})();",
                handled -> {
                    if (!"true".equals(handled)) {
                        performDefaultBackNavigation();
                    }
                }
        );
    }

    private void performDefaultBackNavigation() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == SAVE_DOWNLOAD_REQUEST) {
            HubFileDownload.Result ready = pendingDownload;
            pendingDownload = null;
            if (ready == null) return;
            Uri destination = resultCode == RESULT_OK && data != null ? data.getData() : null;
            if (destination == null) { ready.file.delete(); return; }
            downloadExecutor.execute(() -> {
                try { copyDownloadedFile(ready.file, destination); downloadNotice("文件已保存"); }
                catch (IOException | RuntimeException error) { downloadNotice("文件保存失败，请重新下载"); }
                finally { ready.file.delete(); }
            });
            return;
        }
        if (requestCode != FILE_CHOOSER_REQUEST || pendingFileCallback == null) {
            return;
        }
        Uri[] result = null;
        if (resultCode == RESULT_OK) {
            result = selectedUris(data);
        }
        completeFileSelection(result);
    }

    private Uri[] selectedUris(Intent data) {
        if (data == null) return null;
        Set<Uri> selected = new LinkedHashSet<>();
        ClipData clipData = data.getClipData();
        if (clipData != null) {
            for (int index = 0; index < clipData.getItemCount(); index++) {
                Uri uri = clipData.getItemAt(index).getUri();
                if (sharedAttachmentImporter.isExternalContent(uri)) selected.add(uri);
                if (selected.size() >= SharedFilePolicy.MAX_FILES) break;
            }
        }
        if (selected.size() < SharedFilePolicy.MAX_FILES
                && sharedAttachmentImporter.isExternalContent(data.getData())) selected.add(data.getData());
        if (selected.isEmpty()) {
            Uri[] parsed = WebChromeClient.FileChooserParams.parseResult(RESULT_OK, data);
            if (parsed != null) {
                for (Uri uri : parsed) {
                    if (sharedAttachmentImporter.isExternalContent(uri)) selected.add(uri);
                    if (selected.size() >= SharedFilePolicy.MAX_FILES) break;
                }
            }
        }
        return selected.isEmpty() ? null : selected.toArray(new Uri[0]);
    }

    private boolean completeFileSelection(Uri[] result) {
        ValueCallback<Uri[]> callback = pendingFileCallback;
        if (callback == null) return false;
        boolean samePage = webView != null && pendingFilePage.equals(webView.getUrl())
                && pendingFileGeneration == filePageGeneration
                && HubEndpoint.isInternal(this, Uri.parse(pendingFilePage));
        pendingFileCallback = null;
        pendingFilePage = "";
        callback.onReceiveValue(samePage ? result : null);
        if (samePage && result != null && result.length > 0) scheduleLocalFilePreviews(result);
        return samePage;
    }

    private void restoreSharedFiles(Bundle state) {
        ArrayList<String> imported = state == null ? null : state.getStringArrayList("imported_shared_files");
        if (imported != null) for (String value : imported) {
            Uri uri = Uri.parse(value);
            if (sharedAttachmentImporter.exists(uri)) importedSharedFiles.add(uri);
        }
        // WebView File objects are not restored by saveState. Make retained copies
        // selectable again instead of leaving inaccessible files in the cache quota.
        returnSharedFilesToQueue();
        sharedAttachmentImporter.removeUnreferenced(importedSharedFiles);
    }

    private void returnSharedFilesToQueue() {
        for (Uri uri : importedSharedFiles) {
            if (!pendingSharedFiles.contains(uri)) pendingSharedFiles.add(uri);
        }
    }

    @SuppressWarnings("deprecation")
    private boolean receiveSharedFiles(Intent intent) {
        if (intent == null) return false;
        String action = intent.getAction();
        boolean viewFile = Intent.ACTION_VIEW.equals(action)
                && intent.getData() != null && !HubEndpoint.isInternal(this, intent.getData());
        if (!Intent.ACTION_SEND.equals(action) && !Intent.ACTION_SEND_MULTIPLE.equals(action)
                && !viewFile) return false;
        if ((intent.getFlags() & Intent.FLAG_GRANT_READ_URI_PERMISSION) == 0) {
            showImportMessage("未获得文件读取权限，请在微信文件中重新选择“其他应用打开 → Codex Hub”。");
            return true;
        }
        if (sharedImportInProgress) {
            showImportMessage("文件正在导入，请稍后再次分享。");
            return true;
        }
        Set<Uri> sources = new LinkedHashSet<>();
        try {
            if (Intent.ACTION_SEND.equals(action)) {
                Object stream = intent.getParcelableExtra(Intent.EXTRA_STREAM);
                if (stream instanceof Uri) sources.add((Uri) stream);
            } else if (Intent.ACTION_SEND_MULTIPLE.equals(action)) {
                ArrayList<?> streams = intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
                if (streams != null) for (Object stream : streams) {
                    if (stream instanceof Uri) sources.add((Uri) stream);
                    if (sources.size() > SharedFilePolicy.MAX_FILES) break;
                }
            }
            if (intent.getData() != null) sources.add(intent.getData());
            ClipData clip = intent.getClipData();
            if (clip != null) for (int index = 0; index < clip.getItemCount(); index++) {
                Uri uri = clip.getItemAt(index).getUri();
                if (uri != null) sources.add(uri);
                if (sources.size() > SharedFilePolicy.MAX_FILES) break;
            }
        } catch (RuntimeException exception) {
            showImportMessage("无法读取分享内容，请重新从文件的“其他应用打开”入口导入。");
            return true;
        }
        if (sources.isEmpty()) {
            showImportMessage("没有收到文件。请打开微信聊天文件，再选择“其他应用打开 → Codex Hub”。");
            return true;
        }
        if (sources.size() + importedSharedFiles.size() > SharedFilePolicy.MAX_FILES) {
            showImportMessage("一次最多保留 10 个导入文件，请先发送或清空已有附件。");
            return true;
        }
        for (Uri uri : sources) {
            if (!sharedAttachmentImporter.isExternalContent(uri)
                    || checkUriPermission(uri, android.os.Process.myPid(), android.os.Process.myUid(),
                    Intent.FLAG_GRANT_READ_URI_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
                showImportMessage("来源未提供可读取的文件，请先保存到手机“下载”，再从 Hub 选取。");
                return true;
            }
        }
        sharedImportInProgress = true;
        long generation = sharedImportGeneration;
        String fallbackType = intent.getType();
        showImportMessage("正在接收文件，仅保存在本机，尚未发送。");
        localDataExecutor.execute(() -> {
            ArrayList<Uri> copied = new ArrayList<>();
            int failures = 0;
            for (Uri source : sources) {
                try { copied.add(sharedAttachmentImporter.copy(source, fallbackType)); }
                catch (IOException | RuntimeException exception) { failures++; }
                if (Thread.currentThread().isInterrupted()) break;
            }
            int failedCount = failures;
            runOnUiThread(() -> {
                sharedImportInProgress = false;
                if (activityDestroyed || generation != sharedImportGeneration) {
                    for (Uri uri : copied) sharedAttachmentImporter.remove(uri);
                    return;
                }
                importedSharedFiles.addAll(copied);
                pendingSharedFiles.addAll(copied);
                if (!copied.isEmpty()) {
                    showImportMessage("已接收 " + copied.size()
                            + " 个文件。进入目标项目，点附件 → 微信 / 其他应用待导入，再点发送。"
                            + (failedCount > 0 ? " 部分文件未导入，请检查大小或来源权限。" : ""));
                } else {
                    showImportMessage("未能导入文件。单文件上限 50 MiB、总缓存 100 MiB；请检查文件大小和来源权限。");
                }
            });
        });
        return true;
    }

    private void showImportMessage(String message) {
        if (!activityDestroyed) Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private void showFileSources(WebChromeClient.FileChooserParams params) {
        ArrayList<String> labels = new ArrayList<>();
        if (!pendingSharedFiles.isEmpty()) labels.add("微信 / 其他应用待导入（" + pendingSharedFiles.size() + " 个）");
        labels.add("手机文件（下载 / 文件管理）");
        labels.add("从其他应用选取");
        labels.add("微信聊天文件：如何选取");
        boolean hasPending = !pendingSharedFiles.isEmpty();
        AlertDialog.Builder dialog = new AlertDialog.Builder(this)
                .setTitle("选择附件来源")
                .setItems(labels.toArray(new String[0]), (ignored, index) -> {
                    if (hasPending && index == 0) {
                        int count = params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE
                                ? pendingSharedFiles.size() : Math.min(1, pendingSharedFiles.size());
                        ArrayList<Uri> selected = new ArrayList<>(pendingSharedFiles.subList(0, count));
                        if (completeFileSelection(selected.toArray(new Uri[0]))) pendingSharedFiles.removeAll(selected);
                        return;
                    }
                    int source = index - (hasPending ? 1 : 0);
                    if (source == 2) {
                        completeFileSelection(null);
                        showWeChatFileHelp();
                    } else {
                        Intent picker = params.createIntent();
                        picker.setAction(source == 0 ? Intent.ACTION_OPEN_DOCUMENT : Intent.ACTION_GET_CONTENT);
                        picker.addCategory(Intent.CATEGORY_OPENABLE);
                        picker.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        picker.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,
                                params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE);
                        try {
                            startActivityForResult(Intent.createChooser(picker, "选择文件来源"), FILE_CHOOSER_REQUEST);
                        } catch (ActivityNotFoundException | SecurityException exception) {
                            completeFileSelection(null);
                            showImportMessage("没有可用的文件选择器，请从微信使用“其他应用打开”。");
                        }
                    }
                })
                .setNegativeButton("取消", (ignored, which) -> completeFileSelection(null))
                .setOnCancelListener(ignored -> completeFileSelection(null));
        if (hasPending) dialog.setNeutralButton("清空待导入", (ignored, which) -> {
            completeFileSelection(null);
            ArrayList<Uri> removed = new ArrayList<>(pendingSharedFiles);
            importedSharedFiles.removeAll(removed);
            pendingSharedFiles.clear();
            localDataExecutor.execute(() -> { for (Uri uri : removed) sharedAttachmentImporter.remove(uri); });
            showImportMessage("待导入文件已清空");
        });
        dialog.show();
    }

    private void showWeChatFileHelp() {
        new AlertDialog.Builder(this)
                .setTitle("选择微信聊天文件")
                .setMessage("在微信中打开并下载聊天文件，点右上角菜单 → 其他应用打开（或分享）→ Codex Hub。\n\n"
                        + "回到目标项目，点附件 → 微信 / 其他应用待导入。检查附件后手动发送。\n\n"
                        + "Android 不允许直接浏览微信私有聊天目录；若微信没有提供该入口，请先将文件保存到手机“下载”。")
                .setPositiveButton("打开微信", (ignored, which) -> {
                    Intent launch = getPackageManager().getLaunchIntentForPackage("com.tencent.mm");
                    try {
                        if (launch == null) throw new ActivityNotFoundException();
                        startActivity(launch);
                    } catch (ActivityNotFoundException | SecurityException exception) {
                        showImportMessage("未找到可打开的微信，请手动进入微信选择文件。");
                    }
                })
                .setNegativeButton("知道了", null)
                .show();
    }

    private void scheduleLocalFilePreviews(Uri[] selectedUris) {
        WebView selectedView = webView;
        String selectedPage = selectedView == null ? null : selectedView.getUrl();
        long selectedGeneration = filePageGeneration;
        Uri[] uris = selectedUris.clone();
        localDataExecutor.execute(() -> {
            JSONArray files = new JSONArray();
            for (int index = 0; index < uris.length; index++) {
                files.put(describeSelectedFile(uris[index], index < MAX_LOCAL_PREVIEWS));
            }
            String script = "window.CodexHubAndroidFiles"
                    + "&&window.CodexHubAndroidFiles.receiveSelection("
                    + files.toString() + ");";
            runOnUiThread(() -> {
                if (selectedView != null && selectedView == webView
                        && selectedGeneration == filePageGeneration && selectedPage != null
                        && selectedPage.equals(selectedView.getUrl())) {
                    selectedView.evaluateJavascript(script, null);
                }
            });
        });
    }

    private JSONObject describeSelectedFile(Uri uri, boolean includePreview) {
        JSONObject description = new JSONObject();
        String name = uri.getLastPathSegment() == null ? "attachment" : uri.getLastPathSegment();
        long size = -1L;
        try (
                Cursor cursor = getContentResolver().query(
                        uri,
                        new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE},
                        null,
                        null,
                        null
                )
        ) {
            if (cursor != null && cursor.moveToFirst()) {
                int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
                if (nameIndex >= 0 && !cursor.isNull(nameIndex)) name = cursor.getString(nameIndex);
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) size = cursor.getLong(sizeIndex);
            }
        } catch (RuntimeException exception) {
            Log.w(TAG, "Unable to read selected file metadata", exception);
        }

        String contentType = "";
        try {
            String detectedType = getContentResolver().getType(uri);
            if (detectedType != null) contentType = detectedType;
        } catch (RuntimeException exception) {
            Log.w(TAG, "Unable to read selected file type", exception);
        }
        try {
            description.put("name", name);
            description.put("size", size);
            description.put("type", contentType);
            if ("content".equals(uri.getScheme()) && SharedAttachmentProvider.authority(this).equals(uri.getAuthority())) {
                description.put("importId", uri.toString());
            }
            if (includePreview && isSelectedImage(name, contentType)) {
                String preview = createLocalImagePreview(uri);
                if (!preview.isEmpty()) description.put("preview", preview);
            }
        } catch (JSONException exception) {
            Log.w(TAG, "Unable to encode selected file metadata", exception);
        }
        return description;
    }

    private boolean isSelectedImage(String name, String contentType) {
        if (contentType.toLowerCase(Locale.ROOT).startsWith("image/")) return true;
        String normalized = name.toLowerCase(Locale.ROOT);
        return normalized.endsWith(".avif")
                || normalized.endsWith(".bmp")
                || normalized.endsWith(".gif")
                || normalized.endsWith(".heic")
                || normalized.endsWith(".heif")
                || normalized.endsWith(".jpeg")
                || normalized.endsWith(".jpg")
                || normalized.endsWith(".png")
                || normalized.endsWith(".webp");
    }

    private String createLocalImagePreview(Uri uri) {
        Bitmap bitmap = null;
        Bitmap scaled = null;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                bitmap = getContentResolver().loadThumbnail(
                        uri,
                        new Size(LOCAL_PREVIEW_EDGE_PX, LOCAL_PREVIEW_EDGE_PX),
                        null
                );
            } else {
                BitmapFactory.Options bounds = new BitmapFactory.Options();
                bounds.inJustDecodeBounds = true;
                try (InputStream stream = getContentResolver().openInputStream(uri)) {
                    BitmapFactory.decodeStream(stream, null, bounds);
                }
                BitmapFactory.Options options = new BitmapFactory.Options();
                options.inSampleSize = previewSampleSize(bounds.outWidth, bounds.outHeight);
                try (InputStream stream = getContentResolver().openInputStream(uri)) {
                    bitmap = BitmapFactory.decodeStream(stream, null, options);
                }
            }
            if (bitmap == null) return "";
            scaled = scalePreview(bitmap);
            Bitmap output = scaled == null ? bitmap : scaled;
            boolean preserveAlpha = output.hasAlpha();
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            output.compress(
                    preserveAlpha ? Bitmap.CompressFormat.PNG : Bitmap.CompressFormat.JPEG,
                    preserveAlpha ? 100 : 80,
                    bytes
            );
            String mime = preserveAlpha ? "image/png" : "image/jpeg";
            return "data:" + mime + ";base64,"
                    + Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP);
        } catch (IOException | RuntimeException exception) {
            Log.w(TAG, "Unable to create local image preview", exception);
            return "";
        } finally {
            if (scaled != null && scaled != bitmap) scaled.recycle();
            if (bitmap != null) bitmap.recycle();
        }
    }

    private int previewSampleSize(int width, int height) {
        int sample = 1;
        while (width / sample > LOCAL_PREVIEW_EDGE_PX * 2
                || height / sample > LOCAL_PREVIEW_EDGE_PX * 2) {
            sample *= 2;
        }
        return sample;
    }

    private Bitmap scalePreview(Bitmap source) {
        int width = source.getWidth();
        int height = source.getHeight();
        int largest = Math.max(width, height);
        if (largest <= LOCAL_PREVIEW_EDGE_PX) return source;
        float ratio = (float) LOCAL_PREVIEW_EDGE_PX / (float) largest;
        return Bitmap.createScaledBitmap(
                source,
                Math.max(1, Math.round(width * ratio)),
                Math.max(1, Math.round(height * ratio)),
                true
        );
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode,
            String[] permissions,
            int[] grantResults
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == RECORD_AUDIO_PERMISSION_REQUEST) {
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (granted && startListeningAfterPermission) {
                startListeningAfterPermission = false;
                startVoiceRecognition();
            } else {
                startListeningAfterPermission = false;
                notifyVoiceError("需要麦克风权限才能进行语音输入");
            }
        } else if (requestCode == NOTIFICATION_PERMISSION_REQUEST) {
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (!granted) {
                Toast.makeText(
                        this,
                        "未开启通知权限，新消息不会响铃；可在系统设置中为 Codex Hub 开启通知。",
                        Toast.LENGTH_LONG
                ).show();
            }
        }
    }

    @Override
    protected void onDestroy() {
        activityDestroyed = true;
        sharedImportGeneration += 1L;
        resumeProbeGeneration += 1L;
        mainFrameRetryGeneration += 1L;
        unregisterNetworkCallback();
        closeImageViewer();
        if (pendingFileCallback != null) {
            pendingFileCallback.onReceiveValue(null);
            pendingFileCallback = null;
        }
        if (speechRecognizer != null) {
            speechRecognizer.destroy();
            speechRecognizer = null;
        }
        if (textToSpeech != null) {
            textToSpeech.stop();
            textToSpeech.shutdown();
            textToSpeech = null;
        }
        if (webView != null) {
            webView.removeJavascriptInterface("HubVoice");
            webView.removeJavascriptInterface("HubMedia");
            webView.removeJavascriptInterface("HubLocalData");
            webView.stopLoading();
            if (rootView != null) rootView.removeView(webView);
            webView.removeAllViews();
            webView.destroy();
            webView = null;
        }
        localDataExecutor.shutdownNow();
        downloadExecutor.shutdownNow();
        if (pendingDownload != null) { pendingDownload.file.delete(); pendingDownload = null; }
        if (!isChangingConfigurations()) {
            sharedAttachmentImporter.removeUnreferenced(java.util.Collections.emptyList());
        }
        super.onDestroy();
    }

    private void configureVoice() {
        if (textToSpeech != null) return;
        textToSpeech = new TextToSpeech(this, status -> {
            if (status != TextToSpeech.SUCCESS || textToSpeech == null) {
                textToSpeechReady = false;
                pendingSpeechText = "";
                notifyVoiceError("设备朗读服务初始化失败");
                return;
            }
            int result = textToSpeech.setLanguage(Locale.SIMPLIFIED_CHINESE);
            textToSpeechReady = result != TextToSpeech.LANG_MISSING_DATA
                    && result != TextToSpeech.LANG_NOT_SUPPORTED;
            if (!textToSpeechReady) {
                pendingSpeechText = "";
                notifyVoiceError("设备朗读服务不支持中文");
                return;
            }
            String queued = pendingSpeechText;
            pendingSpeechText = "";
            if (!queued.isEmpty()) speakPreparedText(queued);
        });
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQUEST
            );
        }
    }

    private void scheduleAuthenticatedBackgroundWork() {
        if (!activityResumed || rootView == null || !hasHubLoginCookies()) return;
        if (!notificationPermissionRequested) {
            notificationPermissionRequested = true;
            rootView.postDelayed(() -> {
                if (activityResumed && hasHubLoginCookies() && !isFinishing()) {
                    requestNotificationPermission();
                } else {
                    notificationPermissionRequested = false;
                }
            }, 600L);
        }
        if (!backgroundNotificationsStarted && !backgroundNotificationsScheduled) {
            backgroundNotificationsScheduled = true;
            rootView.postDelayed(this::startBackgroundNotifications, 1200L);
        }
    }

    private boolean hasHubLoginCookies() {
        String cookies;
        try {
            cookies = CookieManager.getInstance().getCookie(BuildConfig.HUB_URL);
        } catch (RuntimeException exception) {
            Log.w(TAG, "Unable to inspect Hub login state", exception);
            return false;
        }
        return cookies != null && !cookies.trim().isEmpty();
    }

    private void startBackgroundNotifications() {
        backgroundNotificationsScheduled = false;
        if (backgroundNotificationsStarted || !activityResumed
                || !hasHubLoginCookies() || isFinishing()) return;
        Intent intent = new Intent(this, HubNotificationService.class);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
            backgroundNotificationsStarted = true;
        } catch (IllegalStateException exception) {
            Log.w(TAG, "Unable to start Hub notification service", exception);
        }
    }

    private void requestVoiceRecognition() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            startListeningAfterPermission = true;
            requestPermissions(
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    RECORD_AUDIO_PERMISSION_REQUEST
            );
            return;
        }
        startVoiceRecognition();
    }

    private void startVoiceRecognition() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            notifyVoiceError("当前设备没有可用的语音识别服务");
            return;
        }
        if (speechRecognizer == null) {
            speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this);
            speechRecognizer.setRecognitionListener(new HubRecognitionListener());
        }
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
        );
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN");
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "请说出要发送到 Hub 的内容");
        notifyVoiceState(true, "正在听，请说话");
        speechRecognizer.startListening(intent);
    }

    private void stopVoiceRecognition() {
        if (speechRecognizer != null) {
            speechRecognizer.stopListening();
        }
        notifyVoiceState(false, "语音输入已停止");
    }

    private void speakText(String rawText) {
        String text = rawText == null ? "" : rawText.trim();
        if (text.isEmpty()) return;
        if (text.length() > 3000) {
            text = text.substring(0, 3000);
        }
        if (textToSpeech == null) {
            pendingSpeechText = text;
            notifyVoiceState(false, "正在准备设备朗读…");
            configureVoice();
            return;
        }
        if (!textToSpeechReady) {
            pendingSpeechText = text;
            return;
        }
        speakPreparedText(text);
    }

    private void speakPreparedText(String text) {
        if (textToSpeech == null || !textToSpeechReady || text.isEmpty()) return;
        textToSpeech.speak(text, TextToSpeech.QUEUE_FLUSH, null, "hub-reply");
    }

    private void stopSpeaking() {
        pendingSpeechText = "";
        if (textToSpeech != null) {
            textToSpeech.stop();
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void openImageViewer(String rawUrl, String rawTitle) {
        Uri uri;
        try {
            uri = Uri.parse(rawUrl == null ? "" : rawUrl);
        } catch (RuntimeException exception) {
            Toast.makeText(this, "图片地址无效", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!isTrustedImageUrl(uri)) {
            Toast.makeText(this, "已拦截非 Hub 图片地址", Toast.LENGTH_SHORT).show();
            return;
        }
        closeImageViewer();

        String title = rawTitle == null || rawTitle.trim().isEmpty()
                ? "图片预览"
                : rawTitle.trim();
        Dialog dialog = new Dialog(this, android.R.style.Theme_DeviceDefault_Light_NoActionBar);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(9, 13, 20));

        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(8), dp(6), dp(8), dp(6));
        toolbar.setBackgroundColor(Color.rgb(20, 27, 38));

        Button back = new Button(this);
        back.setText("返回");
        back.setOnClickListener(view -> dialog.dismiss());
        toolbar.addView(back, new LinearLayout.LayoutParams(dp(72), dp(44)));

        TextView heading = new TextView(this);
        heading.setText(title);
        heading.setTextColor(Color.WHITE);
        heading.setTextSize(14);
        heading.setSingleLine(true);
        heading.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams headingLayout = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f
        );
        headingLayout.setMargins(dp(8), 0, dp(8), 0);
        toolbar.addView(heading, headingLayout);

        Button share = new Button(this);
        share.setText("分享");
        share.setOnClickListener(view -> shareMedia(uri.toString(), title));
        toolbar.addView(share, new LinearLayout.LayoutParams(dp(72), dp(44)));
        root.addView(toolbar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        WebView viewer = new WebView(this);
        viewer.setBackgroundColor(Color.rgb(4, 7, 13));
        WebSettings settings = viewer.getSettings();
        settings.setJavaScriptEnabled(false);
        settings.setDomStorageEnabled(false);
        settings.setDatabaseEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        settings.setSupportZoom(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }
        viewer.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri target = request.getUrl();
                if (isTrustedImageUrl(target)) return false;
                openExternal(target);
                return true;
            }

            @SuppressLint("NewApi")
            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                Log.e(
                        TAG,
                        "Image viewer renderer exited; closing preview. crashed="
                                + detail.didCrash()
                );
                if (imageViewerWebView == view) imageViewerWebView = null;
                Dialog crashedDialog = imageViewerDialog;
                imageViewerDialog = null;
                if (crashedDialog != null && crashedDialog.isShowing()) {
                    crashedDialog.dismiss();
                }
                view.destroy();
                return true;
            }
        });
        String imageUrl = htmlEscape(uri.toString());
        String imageTitle = htmlEscape(title);
        String html = "<!doctype html><html><head><meta charset=\"utf-8\">"
                + "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,"
                + "maximum-scale=8,user-scalable=yes\"><style>"
                + "html,body{margin:0;width:100%;height:100%;background:#04070d;color:#fff}"
                + "body{display:flex;align-items:center;justify-content:center;overflow:auto}"
                + "img{display:block;max-width:100%;height:auto;margin:auto}"
                + "</style></head><body><img src=\"" + imageUrl + "\" alt=\""
                + imageTitle + "\"></body></html>";
        viewer.loadDataWithBaseURL(HubEndpoint.current(this), html, "text/html", "UTF-8", null);
        root.addView(viewer, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
        ));

        dialog.setContentView(root);
        dialog.setOnDismissListener(ignored -> {
            if (imageViewerWebView == viewer) {
                viewer.stopLoading();
                viewer.destroy();
                imageViewerWebView = null;
            }
            if (imageViewerDialog == dialog) imageViewerDialog = null;
        });
        imageViewerDialog = dialog;
        imageViewerWebView = viewer;
        dialog.show();
        if (dialog.getWindow() != null) {
            dialog.getWindow().setLayout(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT
            );
            dialog.getWindow().setStatusBarColor(Color.rgb(20, 27, 38));
            dialog.getWindow().setNavigationBarColor(Color.rgb(4, 7, 13));
        }
    }

    private void closeImageViewer() {
        Dialog dialog = imageViewerDialog;
        if (dialog != null) {
            imageViewerDialog = null;
            if (dialog.isShowing()) dialog.dismiss();
        }
        WebView viewer = imageViewerWebView;
        if (viewer != null) {
            imageViewerWebView = null;
            viewer.stopLoading();
            viewer.destroy();
        }
    }

    private boolean isTrustedImageUrl(Uri uri) {
        if (!isInternalUrl(uri)) return false;
        String path = uri.getPath();
        return path != null && path.contains("/projects/") && path.contains("/attachments/");
    }

    private void shareMedia(String url, String title) {
        Intent shareIntent = new Intent(Intent.ACTION_SEND);
        shareIntent.setType("text/plain");
        shareIntent.putExtra(Intent.EXTRA_TEXT, url);
        if (title != null && !title.trim().isEmpty()) {
            shareIntent.putExtra(Intent.EXTRA_SUBJECT, title.trim());
        }
        try {
            startActivity(Intent.createChooser(shareIntent, "分享媒体"));
        } catch (ActivityNotFoundException exception) {
            Log.w(TAG, "No app is available to share media", exception);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private static String htmlEscape(String value) {
        return String.valueOf(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;")
                .replace("'", "&#39;");
    }

    private void notifyVoiceTranscript(String transcript, boolean isFinal) {
        evaluateVoiceScript(
                "window.CodexHubVoice&&window.CodexHubVoice.receiveTranscript("
                        + JSONObject.quote(transcript) + "," + isFinal + ");"
        );
    }

    private void notifyVoiceState(boolean active, String message) {
        evaluateVoiceScript(
                "window.CodexHubVoice&&window.CodexHubVoice.setListeningState("
                        + active + "," + JSONObject.quote(message) + ");"
        );
    }

    private void notifyVoiceError(String message) {
        evaluateVoiceScript(
                "window.CodexHubVoice&&window.CodexHubVoice.receiveError("
                        + JSONObject.quote(message) + ");"
        );
    }

    private void evaluateVoiceScript(String script) {
        if (webView == null) return;
        runOnUiThread(() -> webView.evaluateJavascript(script, null));
    }

    private String initialUrl(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data != null && isInternalUrl(data)) {
            return data.toString();
        }
        String lastUrl = localData == null ? "" : localData.getString(LAST_INTERNAL_URL, "");
        if (!lastUrl.isEmpty()) {
            Uri lastUri = Uri.parse(lastUrl);
            if (isRestorableProjectUrl(lastUri)) return lastUrl;
        }
        return HubEndpoint.current(this);
    }

    private boolean isRestorableProjectUrl(Uri uri) {
        return uri != null && HubNavigationPolicy.workspace(HubEndpoint.current(this), uri.toString());
    }

    private String draftPreferenceKey(String rawKey) {
        String key = rawKey == null ? "" : rawKey.trim();
        if (key.length() > 500) key = key.substring(0, 500);
        return "draft:" + key;
    }

    private String readTextAsset(String assetName) {
        StringBuilder text = new StringBuilder();
        try (
                InputStream stream = getAssets().open(assetName);
                BufferedReader reader = new BufferedReader(
                        new InputStreamReader(stream, StandardCharsets.UTF_8)
                )
        ) {
            String line;
            while ((line = reader.readLine()) != null) {
                text.append(line).append('\n');
            }
        } catch (IOException exception) {
            Log.e(TAG, "Unable to load Android UI asset: " + assetName, exception);
        }
        return text.toString();
    }

    private final class HubWebViewClient extends WebViewClient {
        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            super.onPageStarted(view, url, favicon);
            if (view == webView) {
                filePageGeneration += 1L;
                returnSharedFilesToQueue();
                completeFileSelection(null);
            }
            if (view == webView) {
                mainFrameRetryGeneration += 1L;
                primaryLoadInProgress = true;
                primaryPageReady = false;
                primaryMainFrameFailed = false;
            }
            if (view == webView && connectionRecoveryView != null
                    && connectionRecoveryView.getVisibility() == View.VISIBLE) {
                showConnectionRecovery("正在重新连接…");
            }
        }

        @Override
        public void onPageCommitVisible(WebView view, String url) {
            super.onPageCommitVisible(view, url);
            if (view == webView && !primaryMainFrameFailed && isInternalUrl(Uri.parse(url))) {
                if (HubNavigationPolicy.recoverable(HubEndpoint.current(MainActivity.this), url)) {
                    lastCommittedInternalUrl = url;
                }
                markPrimaryPageVisible();
                injectAndroidCss(view);
            }
        }

        @Override
        public void onReceivedError(
                WebView view,
                WebResourceRequest request,
                WebResourceError error
        ) {
            super.onReceivedError(view, request, error);
            if (view != webView || request == null || !request.isForMainFrame()) return;
            String detail = error == null ? "unknown" : String.valueOf(error.getDescription());
            if (detail.contains("ERR_ABORTED")) return;
            handleMainFrameLoadFailure(detail);
        }

        @Override
        public void onReceivedHttpError(
                WebView view,
                WebResourceRequest request,
                WebResourceResponse errorResponse
        ) {
            super.onReceivedHttpError(view, request, errorResponse);
            if (view != webView || request == null || !request.isForMainFrame()
                    || errorResponse == null) return;
            int status = errorResponse.getStatusCode();
            if (status == 408 || status == 429 || status >= 500) {
                handleMainFrameLoadFailure("HTTP " + status);
            }
        }

        @Override
        @TargetApi(Build.VERSION_CODES.O)
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            if (view != webView) return false;
            Log.e(
                    TAG,
                    "Hub WebView renderer exited; rebuilding. crashed=" + detail.didCrash()
                            + " priority=" + detail.rendererPriorityAtExit()
            );
            rebuildPrimaryWebView(view);
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            if (view != webView || primaryMainFrameFailed) {
                primaryLoadInProgress = false;
                return;
            }
            Uri uri = Uri.parse(url);
            if (!isInternalUrl(uri)) {
                return;
            }
            markPrimaryPageVisible();
            if (isRestorableProjectUrl(uri) && localData != null) {
                localData.edit().putString(LAST_INTERNAL_URL, url).apply();
            }
            CookieManager.getInstance().flush();
            injectAndroidCss(view);
            String script = "window.CodexHubAndroidVersion="
                    + JSONObject.quote(BuildConfig.VERSION_NAME) + ";"
                    + androidWorkspaceScript + ";" + androidAppScript + ";" + androidRecoveryScript;
            view.evaluateJavascript(script, null);
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (isInternalUrl(uri)) {
                return false;
            }
            openExternal(uri);
            return true;
        }

        @SuppressWarnings("deprecation")
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            Uri uri = Uri.parse(url);
            if (isInternalUrl(uri)) {
                return false;
            }
            openExternal(uri);
            return true;
        }
    }

    private void injectAndroidCss(WebView view) {
        String script = "(function(){"
                + "if(!document.body)return;"
                + "document.documentElement.classList.add('codex-hub-android');"
                + "if(document.querySelector('[data-ws-shell]')){"
                + "document.getElementById('" + MOBILE_LAYOUT_STYLE_ID + "')?.remove();"
                + "document.body.classList.remove('android-chat-mode','android-tools-mode');return;}"
                + "if(document.body.classList.contains('hub-detail-screen'))"
                + "document.body.classList.add('android-chat-mode');"
                + "var s=document.getElementById('" + MOBILE_LAYOUT_STYLE_ID + "');"
                + "if(!s){s=document.createElement('style');s.id='" + MOBILE_LAYOUT_STYLE_ID
                + "';document.head.appendChild(s);}"
                + "s.textContent=" + JSONObject.quote(androidAppCss) + ";"
                + "})();";
        view.evaluateJavascript(script, null);
    }

    private final class HubVoiceBridge {
        @JavascriptInterface
        public void startListening() {
            runOnUiThread(MainActivity.this::requestVoiceRecognition);
        }

        @JavascriptInterface
        public void stopListening() {
            runOnUiThread(MainActivity.this::stopVoiceRecognition);
        }

        @JavascriptInterface
        public void speak(String text) {
            runOnUiThread(() -> speakText(text));
        }

        @JavascriptInterface
        public void stopSpeaking() {
            runOnUiThread(MainActivity.this::stopSpeaking);
        }
    }

    private final class HubMediaBridge {
        @JavascriptInterface
        public void openImage(String url, String title) {
            runOnUiThread(() -> openImageViewer(url, title));
        }

        @JavascriptInterface
        public void haptic() {
            runOnUiThread(() -> {
                if (webView != null) {
                    webView.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP);
                }
            });
        }

        @JavascriptInterface
        public void share(String url, String title) {
            runOnUiThread(() -> shareMedia(url, title));
        }
    }

    private final class HubAppBridge {
        @JavascriptInterface
        public void download(String url) {
            runOnUiThread(() -> {
                if (webView == null || activityDestroyed
                        || !HubEndpoint.isInternal(MainActivity.this, Uri.parse(String.valueOf(webView.getUrl())))) return;
                startHubDownload(url, webView.getSettings().getUserAgentString());
            });
        }

        @JavascriptInterface
        public void openServerSettings() {
            runOnUiThread(MainActivity.this::showServerSettings);
        }

        @JavascriptInterface
        public String currentServer() {
            return HubEndpoint.current(MainActivity.this);
        }
    }

    private final class HubLocalDataBridge {
        @JavascriptInterface
        public void clearImportedFilesFor(String rawIds) {
            if (rawIds == null || rawIds.length() > 16384) return;
            final ArrayList<String> ids = new ArrayList<>();
            try {
                JSONArray values = new JSONArray(rawIds);
                for (int index = 0; index < Math.min(values.length(), SharedFilePolicy.MAX_FILES); index++) {
                    ids.add(values.optString(index, ""));
                }
            } catch (JSONException error) { return; }
            runOnUiThread(() -> {
                if (activityDestroyed || webView == null
                        || !HubEndpoint.isInternal(MainActivity.this, Uri.parse(String.valueOf(webView.getUrl())))) return;
                ArrayList<Uri> consumed = new ArrayList<>();
                for (Uri uri : importedSharedFiles) {
                    if (ids.contains(uri.toString()) && !pendingSharedFiles.contains(uri)) consumed.add(uri);
                }
                importedSharedFiles.removeAll(consumed);
                localDataExecutor.execute(() -> { for (Uri uri : consumed) sharedAttachmentImporter.remove(uri); });
            });
        }

        @JavascriptInterface
        public void clearImportedFiles() {
            runOnUiThread(() -> {
                if (webView == null || !HubEndpoint.isInternal(MainActivity.this, Uri.parse(webView.getUrl() == null ? "" : webView.getUrl()))) return;
                ArrayList<Uri> consumed = new ArrayList<>(importedSharedFiles);
                consumed.removeAll(pendingSharedFiles);
                importedSharedFiles.removeAll(consumed);
                localDataExecutor.execute(() -> { for (Uri uri : consumed) sharedAttachmentImporter.remove(uri); });
            });
        }

        @JavascriptInterface
        public String loadDraft(String key) {
            if (localData == null) return "";
            return localData.getString(draftPreferenceKey(key), "");
        }

        @JavascriptInterface
        public void saveDraft(String key, String value) {
            if (localData == null) return;
            String draft = value == null ? "" : value;
            if (draft.length() > 100_000) draft = draft.substring(0, 100_000);
            localData.edit().putString(draftPreferenceKey(key), draft).apply();
        }

        @JavascriptInterface
        public void clearDraft(String key) {
            if (localData == null) return;
            localData.edit().remove(draftPreferenceKey(key)).apply();
        }
    }

    private final class HubRecognitionListener implements RecognitionListener {
        @Override
        public void onReadyForSpeech(Bundle params) {
            notifyVoiceState(true, "正在听，请说话");
        }

        @Override public void onBeginningOfSpeech() { }
        @Override public void onRmsChanged(float rmsdB) { }
        @Override public void onBufferReceived(byte[] buffer) { }
        @Override public void onEndOfSpeech() { }

        @Override
        public void onError(int error) {
            notifyVoiceError("语音识别未完成，请重试（" + error + "）");
        }

        @Override
        public void onResults(Bundle results) {
            ArrayList<String> matches = results.getStringArrayList(
                    SpeechRecognizer.RESULTS_RECOGNITION
            );
            if (matches == null || matches.isEmpty()) {
                notifyVoiceError("没有识别到清晰内容，请重试");
                return;
            }
            notifyVoiceTranscript(matches.get(0), true);
        }

        @Override
        public void onPartialResults(Bundle partialResults) {
            ArrayList<String> matches = partialResults.getStringArrayList(
                    SpeechRecognizer.RESULTS_RECOGNITION
            );
            if (matches != null && !matches.isEmpty()) {
                notifyVoiceTranscript(matches.get(0), false);
            }
        }

        @Override public void onEvent(int eventType, Bundle params) { }
    }

    private final class HubWebChromeClient extends WebChromeClient {
        @Override
        public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
            if (consoleMessage == null) return false;
            ConsoleMessage.MessageLevel level = consoleMessage.messageLevel();
            if (level == ConsoleMessage.MessageLevel.WARNING
                    || level == ConsoleMessage.MessageLevel.ERROR) {
                Log.w(
                        TAG,
                        "WebView " + level + " at " + consoleMessage.sourceId()
                                + ":" + consoleMessage.lineNumber() + " — "
                                + consoleMessage.message()
                );
            }
            return true;
        }

        @Override
        public boolean onShowFileChooser(
                WebView webView,
                ValueCallback<Uri[]> filePathCallback,
                FileChooserParams fileChooserParams
        ) {
            if (pendingFileCallback != null) {
                pendingFileCallback.onReceiveValue(null);
            }
            pendingFileCallback = filePathCallback;
            pendingFilePage = webView.getUrl() == null ? "" : webView.getUrl();
            pendingFileGeneration = filePageGeneration;
            if (webView != MainActivity.this.webView || !isRestorableProjectUrl(Uri.parse(pendingFilePage))) {
                completeFileSelection(null);
                return true;
            }
            showFileSources(fileChooserParams);
            return true;
        }
    }

    private boolean isInternalUrl(Uri uri) {
        String scheme = uri == null ? null : uri.getScheme();
        if ("about".equals(scheme)) {
            return "about:blank".equals(uri.toString());
        }
        return HubEndpoint.isInternal(this, uri);
    }

    private void showServerSettings() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(HubEndpoint.current(this));
        input.setSelection(input.getText().length());
        int padding = dp(20);
        FrameLayout container = new FrameLayout(this);
        container.setPadding(padding, dp(8), padding, 0);
        container.addView(input, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Hub 服务器")
                .setMessage("请输入完整 HTTPS 地址。服务器迁移后可在这里切换，无需重装 App。")
                .setView(container)
                .setNegativeButton("取消", null)
                .setNeutralButton("恢复默认", null)
                .setPositiveButton("保存并连接", null)
                .create();
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(button -> {
                if (!applyHubEndpoint(input.getText().toString())) return;
                dialog.dismiss();
            });
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(button -> {
                input.setText(BuildConfig.HUB_URL);
                input.setSelection(input.getText().length());
            });
        });
        dialog.show();
    }

    private boolean applyHubEndpoint(String rawValue) {
        String normalized = HubEndpoint.normalize(rawValue);
        if (normalized == null) {
            Toast.makeText(
                    this,
                    "地址无效：必须是没有账号、参数或片段的完整 HTTPS 地址",
                    Toast.LENGTH_LONG
            ).show();
            return false;
        }
        if (!HubEndpoint.save(this, normalized)) return false;
        sharedImportGeneration += 1L;
        pendingSharedFiles.clear();
        importedSharedFiles.clear();
        localDataExecutor.execute(() -> sharedAttachmentImporter.removeUnreferenced(java.util.Collections.emptyList()));
        lastCommittedInternalUrl = normalized;
        mainFrameFailureCount = 0;
        mainFrameRetryGeneration += 1L;
        if (webView != null) {
            webView.stopLoading();
            webView.clearHistory();
            showConnectionRecovery("正在连接新服务器…");
            loadPrimaryUrl(webView, normalized);
        }
        Toast.makeText(this, "服务器地址已保存", Toast.LENGTH_SHORT).show();
        return true;
    }

    private void startHubDownload(String url, String userAgent) {
        String base = HubEndpoint.current(this);
        if (!HubNavigationPolicy.download(base, url)) {
            downloadNotice("该文件不能由 Hub 直接下载，请打开来源页面");
            return;
        }
        if (activityDestroyed || activeDownloads.contains(url)) return;
        // Older Android releases need one system save picker per file. Do not
        // queue another transfer while that picker could still be pending.
        int downloadLimit = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q ? 3 : 1;
        if (activeDownloads.size() >= downloadLimit || pendingDownload != null) {
            downloadNotice("请等待当前文件下载或保存完成");
            return;
        }
        activeDownloads.add(url);
        String cookie = CookieManager.getInstance().getCookie(url);
        downloadNotice("正在下载，当前对话会保持打开");
        downloadExecutor.execute(() -> {
            HubFileDownload.Result result = null;
            try {
                result = HubFileDownload.fetch(base, url, cookie, userAgent,
                        new File(getCacheDir(), "downloads"),
                        uri -> (java.net.HttpURLConnection) uri.toURL().openConnection());
                if (activityDestroyed || !base.equals(HubEndpoint.current(this))) {
                    result.file.delete();
                    return;
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    saveDownloadToCollection(result);
                    result.file.delete();
                    downloadNotice("已保存到下载目录：" + result.name);
                } else {
                    HubFileDownload.Result ready = result;
                    runOnUiThread(() -> {
                        if (activityDestroyed || pendingDownload != null) { ready.file.delete(); return; }
                        pendingDownload = ready;
                        Intent save = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                        save.addCategory(Intent.CATEGORY_OPENABLE);
                        save.setType(ready.type);
                        save.putExtra(Intent.EXTRA_TITLE, ready.name);
                        try { startActivityForResult(save, SAVE_DOWNLOAD_REQUEST); }
                        catch (ActivityNotFoundException error) {
                            ready.file.delete(); pendingDownload = null;
                            downloadNotice("系统没有文件保存入口，请在电脑下载");
                        }
                    });
                }
            } catch (IOException | RuntimeException error) {
                if (result != null) result.file.delete();
                downloadNotice(error instanceof IOException ? error.getMessage() : "下载未完成，请稍后重试");
            } finally {
                runOnUiThread(() -> activeDownloads.remove(url));
            }
        });
    }

    @TargetApi(Build.VERSION_CODES.Q)
    private void saveDownloadToCollection(HubFileDownload.Result result) throws IOException {
        ContentValues values = new ContentValues();
        values.put(MediaStore.Downloads.DISPLAY_NAME, result.name);
        values.put(MediaStore.Downloads.MIME_TYPE, result.type);
        values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Codex Hub");
        values.put(MediaStore.Downloads.IS_PENDING, 1);
        Uri destination = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (destination == null) throw new IOException("无法打开系统下载目录");
        boolean completed = false;
        try {
            copyDownloadedFile(result.file, destination);
            ContentValues published = new ContentValues();
            published.put(MediaStore.Downloads.IS_PENDING, 0);
            if (getContentResolver().update(destination, published, null, null) != 1) {
                throw new IOException("无法完成下载文件保存");
            }
            completed = true;
        } finally {
            if (!completed) getContentResolver().delete(destination, null, null);
        }
    }

    private void copyDownloadedFile(File file, Uri destination) throws IOException {
        try (InputStream input = new FileInputStream(file);
             OutputStream output = getContentResolver().openOutputStream(destination, "w")) {
            if (output == null) throw new IOException("无法写入下载文件");
            byte[] buffer = new byte[64 * 1024];
            int length;
            while ((length = input.read(buffer)) != -1) {
                if (Thread.currentThread().isInterrupted()) throw new IOException("保存已取消");
                output.write(buffer, 0, length);
            }
            output.flush();
        }
    }

    private void downloadNotice(String message) {
        runOnUiThread(() -> {
            if (!activityDestroyed) Toast.makeText(this, message == null ? "下载未完成，请重试" : message, Toast.LENGTH_LONG).show();
        });
    }

    private void openExternal(Uri uri) {
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException ignored) {
            // Leave unknown schemes inside the app instead of crashing.
        }
    }
}
