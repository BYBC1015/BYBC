package xyz.baicang.codexhub;

import java.net.URI;
import java.util.ArrayList;

/** Pure URL policy shared by navigation recovery and authenticated downloads. */
final class HubNavigationPolicy {
    static boolean internal(String base, String value) {
        try {
            URI root = URI.create(base), candidate = URI.create(value);
            if (!"https".equalsIgnoreCase(candidate.getScheme()) || candidate.getRawUserInfo() != null
                    || root.getHost() == null || candidate.getHost() == null
                    || !root.getHost().equalsIgnoreCase(candidate.getHost())
                    || port(root) != port(candidate)) return false;
            String path = candidate.getPath(), prefix = root.getPath();
            if (path == null || prefix == null || path.indexOf('\\') >= 0) return false;
            for (String part : path.split("/", -1)) if (part.equals(".") || part.equals("..")) return false;
            if (!prefix.endsWith("/")) prefix += "/";
            return path.equals(prefix.substring(0, prefix.length() - 1)) || path.startsWith(prefix);
        } catch (IllegalArgumentException error) { return false; }
    }

    static boolean workspace(String base, String value) {
        if (!internal(base, value) || !relative(base, value).matches(
                "projects/[^/]+(?:/conversations/[1-9][0-9]*)?/?")) return false;
        String query = URI.create(value).getQuery();
        return query == null || !java.util.Arrays.asList(query.split("&")).contains("view=archive");
    }

    static boolean recoverable(String base, String value) {
        if (!internal(base, value)) return false;
        String relative = relative(base, value);
        return !relative.startsWith("api/") && !relative.startsWith("static/")
                && !relative.startsWith("downloads/") && !relative.equals("logout")
                && !relative.matches("projects/[^/]+/attachments/[0-9]+/?");
    }

    static boolean download(String base, String value) {
        if (!internal(base, value)) return false;
        String relative = relative(base, value);
        return relative.matches("projects/[^/]+/attachments/[1-9][0-9]*/?")
                || relative.matches("downloads/[a-z][a-z0-9_-]*/?");
    }

    /** Drop only obsolete layout selectors; retain task/filter/deep-link context. */
    static String currentPage(String base, String value) {
        if (!workspace(base, value)) return value;
        URI uri = URI.create(value);
        ArrayList<String> keep = new ArrayList<>();
        if (uri.getRawQuery() != null) for (String item : uri.getRawQuery().split("&")) {
            String decoded;
            try { decoded = java.net.URLDecoder.decode(item, "UTF-8"); }
            catch (Exception invalid) { decoded = item; }
            if (!decoded.matches("(?i)view=(workbench|legacy)")) keep.add(item);
        }
        return uri.getScheme() + "://" + uri.getRawAuthority() + uri.getRawPath()
                + (keep.isEmpty() ? "" : "?" + String.join("&", keep))
                + (uri.getRawFragment() == null ? "" : "#" + uri.getRawFragment());
    }

    static boolean authentication(String base, String value) {
        return internal(base, value) && relative(base, value).matches("(?:login|logout)/?");
    }

    static boolean trustedBridge(String base, String page, String origin, boolean mainFrame,
                                 boolean ready, String expectedNonce, String nonce) {
        if (!mainFrame || !ready || expectedNonce == null || !expectedNonce.equals(nonce)
                || !workspace(base, page)) return false;
        try {
            URI root = URI.create(base), source = URI.create(origin);
            return "https".equalsIgnoreCase(source.getScheme()) && source.getRawUserInfo() == null
                    && root.getHost().equalsIgnoreCase(source.getHost()) && port(root) == port(source);
        } catch (RuntimeException invalid) { return false; }
    }

    static boolean currentRelease(String release) {
        if (release == null || !release.matches("[0-9]+\\.[0-9]+\\.[0-9]+")) return false;
        try {
            String[] parts = release.split("\\.");
            int major = Integer.parseInt(parts[0]), minor = Integer.parseInt(parts[1]);
            return major > 2 || major == 2 && minor >= 11;
        } catch (NumberFormatException invalid) { return false; }
    }

    private static String relative(String base, String value) {
        String prefix = URI.create(base).getPath();
        if (!prefix.endsWith("/")) prefix += "/";
        String path = URI.create(value).getPath();
        return path.length() < prefix.length() ? "" : path.substring(prefix.length());
    }
    private static int port(URI uri) { return uri.getPort() < 0 ? 443 : uri.getPort(); }
    private HubNavigationPolicy() { }
}
