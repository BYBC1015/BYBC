package xyz.baicang.codexhub;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** No tokens or rendered HTML are persisted in native offline records. */
final class HubLocalRecordPolicy {
    static final long MAX_AGE_MS = 7L * 24 * 60 * 60 * 1000;
    static String session(String cookies) {
        if (cookies == null) return "";
        for (String part : cookies.split(";")) {
            String item = part.trim();
            if (item.startsWith("codex_hub_session=") && item.length() > 18) {
                try {
                    byte[] digest = MessageDigest.getInstance("SHA-256").digest(item.getBytes(StandardCharsets.UTF_8));
                    StringBuilder value = new StringBuilder();
                    for (byte b : digest) value.append(String.format("%02x", b & 255));
                    return value.toString();
                } catch (Exception unavailable) { return ""; }
            }
        }
        return "";
    }

    static boolean readable(String base, String currentUrl, String savedUrl, String session,
                            String savedSession, long savedAt, long now) {
        return !session.isEmpty() && session.equals(savedSession)
                && HubNavigationPolicy.workspace(base, currentUrl)
                && HubNavigationPolicy.currentPage(base, currentUrl).equals(HubNavigationPolicy.currentPage(base, savedUrl))
                && savedAt > 0 && now >= savedAt && now - savedAt <= MAX_AGE_MS;
    }

    static boolean matchesWorkspace(String base, String url, String user, String project, String conversation) {
        if (user == null || !user.matches("[1-9][0-9]*") || !HubNavigationPolicy.workspace(base, url)) return false;
        String path = java.net.URI.create(url).getPath();
        String prefix = java.net.URI.create(base).getPath();
        if (!prefix.endsWith("/")) prefix += "/";
        String[] parts = path.substring(prefix.length()).split("/");
        return parts.length >= 2 && parts[1].equals(project)
                && (parts.length == 2 ? "new".equals(conversation) : parts.length == 4 && parts[3].equals(conversation));
    }
    private HubLocalRecordPolicy() { }
}
