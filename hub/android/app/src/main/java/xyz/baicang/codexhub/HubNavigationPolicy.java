package xyz.baicang.codexhub;

import java.net.URI;

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
        return internal(base, value) && relative(base, value).matches(
                "projects/[^/]+(?:/conversations/[1-9][0-9]*)?/?");
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

    private static String relative(String base, String value) {
        String prefix = URI.create(base).getPath();
        if (!prefix.endsWith("/")) prefix += "/";
        String path = URI.create(value).getPath();
        return path.length() < prefix.length() ? "" : path.substring(prefix.length());
    }
    private static int port(URI uri) { return uri.getPort() < 0 ? 443 : uri.getPort(); }
    private HubNavigationPolicy() { }
}
