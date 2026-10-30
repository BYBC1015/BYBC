package xyz.baicang.codexhub;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public final class HubNotificationService extends Service {
    private static final String TAG = "HubNotifications";
    private static final String SERVICE_CHANNEL_ID = "hub-background";
    // Channel settings are immutable after first creation. A new id migrates
    // existing installs away from the old channel that did not require sound.
    private static final String MESSAGE_CHANNEL_ID = "hub-replies-with-sound-v2";
    private static final int SERVICE_NOTIFICATION_ID = 7001;
    private static final int MESSAGE_NOTIFICATION_BASE = 8000;
    private static final String PREFS = "hub-notifications";
    private static final String PREF_LAST_ENTRY_ID = "last-entry-id";
    private static final String PREF_SESSION_FINGERPRINT = "session-fingerprint";
    private static final long POLL_SECONDS = 30L;
    private static final long INITIAL_POLL_DELAY_SECONDS = 8L;
    private static final int MAX_RESPONSE_CHARS = 1_000_000;
    private static volatile boolean appInForeground = false;

    private final AtomicBoolean pollInFlight = new AtomicBoolean(false);
    private ScheduledExecutorService executor;
    private NotificationManager notificationManager;
    private ConnectivityManager connectivityManager;
    private SharedPreferences preferences;
    private String lastServiceStatus = "";

    public static void setAppInForeground(boolean foreground) {
        appInForeground = foreground;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        notificationManager = (NotificationManager) getSystemService(
                Context.NOTIFICATION_SERVICE
        );
        connectivityManager = (ConnectivityManager) getSystemService(
                Context.CONNECTIVITY_SERVICE
        );
        preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
        createNotificationChannels();
        lastServiceStatus = "后台通知已开启";
        startForeground(
                SERVICE_NOTIFICATION_ID,
                buildServiceNotification(lastServiceStatus)
        );
        executor = Executors.newSingleThreadScheduledExecutor();
        executor.scheduleWithFixedDelay(
                this::pollSafely,
                INITIAL_POLL_DELAY_SECONDS,
                POLL_SECONDS,
                TimeUnit.SECONDS
        );
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void pollSafely() {
        if (!pollInFlight.compareAndSet(false, true)) return;
        try {
            pollForReplies();
        } catch (Exception exception) {
            Log.w(TAG, "Unable to poll Hub for mobile notifications", exception);
            updateServiceNotification("连接暂时中断，将自动重试");
        } finally {
            pollInFlight.set(false);
        }
    }

    private void pollForReplies() throws Exception {
        String hubUrl = HubEndpoint.current(this);
        String cookie = CookieManager.getInstance().getCookie(hubUrl);
        if (cookie == null || cookie.trim().isEmpty()) {
            updateServiceNotification("请打开应用并登录 Hub");
            return;
        }
        if (!hasUsableNetwork()) {
            updateServiceNotification("网络不可用，联网后将自动恢复");
            return;
        }

        String sessionFingerprint = sha256(cookie.trim());
        String previousFingerprint = preferences.getString(PREF_SESSION_FINGERPRINT, "");
        long lastEntryId;
        if (!sessionFingerprint.equals(previousFingerprint)) {
            lastEntryId = 0L;
            preferences.edit()
                    .putString(PREF_SESSION_FINGERPRINT, sessionFingerprint)
                    .putLong(PREF_LAST_ENTRY_ID, 0L)
                    .apply();
        } else {
            lastEntryId = preferences.getLong(PREF_LAST_ENTRY_ID, 0L);
        }
        URL endpoint = new URL(
                hubUrl
                        + "api/mobile/notifications?after_entry_id="
                        + lastEntryId
                        + "&limit=50"
        );
        HttpURLConnection connection = (HttpURLConnection) endpoint.openConnection();
        String body;
        try {
            connection.setConnectTimeout(12000);
            connection.setReadTimeout(12000);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Cookie", cookie);
            connection.setRequestProperty(
                    "User-Agent",
                    "CodexHubAndroid/" + BuildConfig.VERSION_NAME
            );

            int status = connection.getResponseCode();
            if (status == HttpURLConnection.HTTP_UNAUTHORIZED) {
                updateServiceNotification("登录已过期，请打开应用重新登录");
                return;
            }
            if (status != HttpURLConnection.HTTP_OK) {
                throw new IllegalStateException("Hub notification request failed: " + status);
            }
            try (InputStream stream = connection.getInputStream()) {
                body = readText(stream, MAX_RESPONSE_CHARS);
            }
        } finally {
            connection.disconnect();
        }

        JSONObject payload = new JSONObject(body);
        long latestEntryId = payload.optLong("latest_entry_id", lastEntryId);
        JSONArray notifications = payload.optJSONArray("notifications");

        if (lastEntryId == 0L) {
            preferences.edit().putLong(PREF_LAST_ENTRY_ID, latestEntryId).apply();
            updateServiceNotification("后台通知运行中");
            return;
        }

        long highestSeen = lastEntryId;
        if (notifications != null) {
            for (int index = 0; index < notifications.length(); index += 1) {
                JSONObject item = notifications.getJSONObject(index);
                long entryId = item.optLong("entry_id", 0L);
                highestSeen = Math.max(highestSeen, entryId);
                if (!appInForeground) {
                    showReplyNotification(item);
                }
            }
        }
        highestSeen = Math.max(highestSeen, latestEntryId);
        preferences.edit().putLong(PREF_LAST_ENTRY_ID, highestSeen).apply();
        updateServiceNotification("后台通知运行中");
    }

    private void showReplyNotification(JSONObject item) {
        long entryId = item.optLong("entry_id", System.currentTimeMillis());
        String projectSlug = item.optString("project_slug", "");
        String projectName = item.optString("project_name", "Codex Hub");
        String source = item.optString("source_label", "设备");
        String body = item.optString("body", "收到一条新回复");

        Uri target = Uri.parse(HubEndpoint.projectUrl(this, projectSlug));
        Intent openIntent = new Intent(this, MainActivity.class)
                .setAction(Intent.ACTION_VIEW)
                .setData(target)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                (int) (entryId % Integer.MAX_VALUE),
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Notification.Builder builder = notificationBuilder(MESSAGE_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentTitle(projectName + " · " + source)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setColor(Color.rgb(217, 155, 0))
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setPriority(Notification.PRIORITY_HIGH)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setOnlyAlertOnce(false);
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            builder.setSound(defaultNotificationSound())
                    .setVibrate(new long[]{0L, 240L, 120L, 240L});
        }
        notificationManager.notify(
                MESSAGE_NOTIFICATION_BASE + (int) (entryId % 100000),
                builder.build()
        );
    }

    private Notification buildServiceNotification(String status) {
        Intent openIntent = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                SERVICE_NOTIFICATION_ID,
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return notificationBuilder(SERVICE_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
                .setContentTitle("Codex Hub")
                .setContentText(status)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setCategory(Notification.CATEGORY_SERVICE)
                .build();
    }

    private void updateServiceNotification(String status) {
        if (status.equals(lastServiceStatus)) return;
        lastServiceStatus = status;
        notificationManager.notify(
                SERVICE_NOTIFICATION_ID,
                buildServiceNotification(status)
        );
    }

    private Notification.Builder notificationBuilder(String channelId) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            return new Notification.Builder(this, channelId);
        }
        return new Notification.Builder(this);
    }

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel serviceChannel = new NotificationChannel(
                SERVICE_CHANNEL_ID,
                "Hub 后台连接",
                NotificationManager.IMPORTANCE_LOW
        );
        serviceChannel.setDescription("保持 Hub 消息检查运行");
        serviceChannel.setShowBadge(false);
        serviceChannel.setSound(null, null);
        serviceChannel.enableVibration(false);

        NotificationChannel messageChannel = new NotificationChannel(
                MESSAGE_CHANNEL_ID,
                "Hub 新消息（有提示音）",
                NotificationManager.IMPORTANCE_HIGH
        );
        AudioAttributes soundAttributes = new AudioAttributes.Builder()
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_COMMUNICATION_INSTANT)
                .build();
        messageChannel.setDescription("设备回复和任务结果通知，使用系统通知铃声");
        messageChannel.setSound(defaultNotificationSound(), soundAttributes);
        messageChannel.enableVibration(true);
        messageChannel.setVibrationPattern(new long[]{0L, 240L, 120L, 240L});
        messageChannel.enableLights(true);
        messageChannel.setLightColor(Color.rgb(217, 155, 0));
        messageChannel.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
        notificationManager.createNotificationChannel(serviceChannel);
        notificationManager.createNotificationChannel(messageChannel);
    }

    private static Uri defaultNotificationSound() {
        return RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
    }

    private boolean hasUsableNetwork() {
        if (connectivityManager == null) return true;
        Network active = connectivityManager.getActiveNetwork();
        NetworkCapabilities capabilities = connectivityManager.getNetworkCapabilities(active);
        return capabilities != null
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
    }

    private static String sha256(String value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                value.getBytes(StandardCharsets.UTF_8)
        );
        StringBuilder encoded = new StringBuilder(digest.length * 2);
        for (byte item : digest) {
            encoded.append(String.format(java.util.Locale.ROOT, "%02x", item & 0xff));
        }
        return encoded.toString();
    }

    private static String readText(InputStream stream, int maxChars) throws Exception {
        StringBuilder text = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8)
        )) {
            char[] buffer = new char[8192];
            int count;
            while ((count = reader.read(buffer)) != -1) {
                if (text.length() + count > maxChars) {
                    throw new IOException("Hub notification response is too large");
                }
                text.append(buffer, 0, count);
            }
        }
        return text.toString();
    }
}
