package xyz.baicang.codexhub;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import java.util.Locale;

public final class HubEndpoint {
    private static final String LOCAL_DATA_PREFERENCES = "codex_hub_local_data";
    private static final String NOTIFICATION_PREFERENCES = "hub-notifications";
    private static final String HUB_BASE_URL = "hub_base_url";
    private static final String LAST_INTERNAL_URL = "last_internal_url";
    private static final String NOTIFICATION_LAST_ENTRY_ID = "last-entry-id";

    private HubEndpoint() {}

    public static String current(Context context) {
        SharedPreferences preferences = context.getSharedPreferences(
                LOCAL_DATA_PREFERENCES,
                Context.MODE_PRIVATE
        );
        String stored = preferences.getString(HUB_BASE_URL, "");
        String normalized = normalize(stored);
        if (normalized != null) return normalized;
        normalized = normalize(BuildConfig.HUB_URL);
        if (normalized == null) {
            throw new IllegalStateException("BuildConfig.HUB_URL must be a valid HTTPS URL");
        }
        return normalized;
    }

    public static String normalize(String rawValue) {
        String value = rawValue == null ? "" : rawValue.trim();
        if (value.isEmpty() || value.length() > 2048) return null;
        Uri uri = Uri.parse(value);
        if (!"https".equalsIgnoreCase(uri.getScheme())) return null;
        if (uri.getHost() == null || uri.getHost().trim().isEmpty()) return null;
        if (uri.getPort() == 0 || uri.getPort() > 65535) return null;
        if (uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
            return null;
        }
        String authority = uri.getEncodedAuthority();
        if (authority == null || authority.contains("@")) return null;
        String path = uri.getPath();
        if (path == null || path.isEmpty()) path = "/";
        if (path.indexOf('\\') >= 0) return null;
        for (int index = 0; index < path.length(); index++) if (Character.isISOControl(path.charAt(index))) return null;
        if (!path.startsWith("/")) return null;
        for (String segment : path.split("/", -1)) {
            if (".".equals(segment) || "..".equals(segment)) return null;
        }
        while (path.contains("//")) path = path.replace("//", "/");
        if (!path.endsWith("/")) path += "/";
        return "https://" + authority.toLowerCase(Locale.ROOT) + path;
    }

    public static boolean save(Context context, String rawValue) {
        String normalized = normalize(rawValue);
        if (normalized == null) return false;
        context.getSharedPreferences(LOCAL_DATA_PREFERENCES, Context.MODE_PRIVATE)
                .edit()
                .putString(HUB_BASE_URL, normalized)
                .remove(LAST_INTERNAL_URL)
                .apply();
        context.getSharedPreferences(NOTIFICATION_PREFERENCES, Context.MODE_PRIVATE)
                .edit()
                .remove(NOTIFICATION_LAST_ENTRY_ID)
                .apply();
        return true;
    }

    public static boolean reset(Context context) {
        return save(context, BuildConfig.HUB_URL);
    }

    public static boolean isInternal(Context context, Uri candidate) {
        if (candidate == null || !"https".equalsIgnoreCase(candidate.getScheme())) {
            return false;
        }
        if (candidate.getUserInfo() != null) return false;
        Uri configured = Uri.parse(current(context));
        if (configured.getHost() == null || candidate.getHost() == null) return false;
        if (!configured.getHost().equalsIgnoreCase(candidate.getHost())) return false;
        if (effectivePort(configured) != effectivePort(candidate)) return false;
        String basePath = configured.getPath();
        String candidatePath = candidate.getPath();
        if (basePath == null || candidatePath == null) return false;
        return (candidatePath.equals(basePath.substring(0, basePath.length() - 1))
                || candidatePath.startsWith(basePath))
                && HubNavigationPolicy.internal(current(context), candidate.toString());
    }

    public static String projectUrl(Context context, String projectSlug) {
        return current(context) + "projects/" + Uri.encode(projectSlug == null ? "" : projectSlug);
    }

    private static int effectivePort(Uri uri) {
        return uri.getPort() == -1 ? 443 : uri.getPort();
    }
}
